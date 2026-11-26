"""The two line encodings the engines answer in.

- Google's encoded polyline (OSRM, Valhalla, Mapbox, Google): precision 5 or 6.
- HERE's flexible polyline: a header gives the precision, then the same idea
  with a different alphabet and optional third dimension.
"""
from __future__ import annotations


def decode(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    factor = 10.0 ** precision
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    length = len(encoded)
    while index < length:
        values = []
        for _ in range(2):
            result = shift = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]
        lon += values[1]
        coords.append((lat / factor, lon / factor))
    return coords


_FLEX_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_FLEX_VALUE = {char: index for index, char in enumerate(_FLEX_ALPHABET)}


def _flex_unsigned(encoded: str):
    result = shift = 0
    for char in encoded:
        value = _FLEX_VALUE[char]
        result |= (value & 0x1F) << shift
        if value & 0x20:
            shift += 5
        else:
            yield result
            result = shift = 0


def decode_flexible(encoded: str) -> list[tuple[float, float]]:
    numbers = _flex_unsigned(encoded)
    version = next(numbers)
    if version != 1:
        raise ValueError("unknown flexible polyline version")
    header = next(numbers)
    precision = header & 0x0F
    third_dim = (header >> 4) & 0x07
    factor = 10.0 ** precision

    def signed(value: int) -> int:
        return ~(value >> 1) if value & 1 else value >> 1

    coords: list[tuple[float, float]] = []
    lat = lon = 0
    for raw in numbers:
        lat += signed(raw)
        lon += signed(next(numbers))
        if third_dim:
            next(numbers)
        coords.append((lat / factor, lon / factor))
    return coords
