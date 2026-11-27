// A small zip file reader and writer, enough for the Saved routes screen's
// export and import (saved_routes.js). Writing stores files uncompressed
// (route files are small); reading also takes deflated files, so a zip that
// Finder or Windows made again still loads (DecompressionStream).
const Zip = {
  crcTable: null,

  crc32( bytes ) {
    if ( !this.crcTable ) {
      this.crcTable = new Uint32Array( 256 );
      for ( let n = 0 ; n < 256 ; n++ ) {
        let c = n;
        for ( let k = 0 ; k < 8 ; k++ ) c = c & 1 ? 0xedb88320 ^ ( c >>> 1 ) : c >>> 1;
        this.crcTable[ n ] = c >>> 0;
      }
    }
    let crc = 0xffffffff;
    for ( let i = 0 ; i < bytes.length ; i++ ) crc = this.crcTable[ ( crc ^ bytes[ i ] ) & 0xff ] ^ ( crc >>> 8 );
    return ( crc ^ 0xffffffff ) >>> 0;
  },

  // DOS time and date for the headers.
  dosTime( date ) {
    return {
      time: ( date.getHours() << 11 ) | ( date.getMinutes() << 5 ) | Math.floor( date.getSeconds() / 2 ),
      date: ( ( date.getFullYear() - 1980 ) << 9 ) | ( ( date.getMonth() + 1 ) << 5 ) | date.getDate(),
    };
  },

  // files: [{ path , data }] (data a string or Uint8Array) -> a zip Blob.
  build( files ) {
    const encoder = new TextEncoder();
    const when = this.dosTime( new Date() );
    const parts = [];
    const central = [];
    let offset = 0;
    files.forEach( ( file ) => {
      const name = encoder.encode( file.path );
      const data = typeof file.data === "string" ? encoder.encode( file.data ) : file.data;
      const crc = this.crc32( data );
      const local = new DataView( new ArrayBuffer( 30 ) );
      local.setUint32( 0 , 0x04034b50 , true );
      local.setUint16( 4 , 20 , true );
      local.setUint16( 6 , 0x0800 , true ); // names are UTF-8
      local.setUint16( 8 , 0 , true ); // stored
      local.setUint16( 10 , when.time , true );
      local.setUint16( 12 , when.date , true );
      local.setUint32( 14 , crc , true );
      local.setUint32( 18 , data.length , true );
      local.setUint32( 22 , data.length , true );
      local.setUint16( 26 , name.length , true );
      local.setUint16( 28 , 0 , true );
      parts.push( new Uint8Array( local.buffer ) , name , data );
      const entry = new DataView( new ArrayBuffer( 46 ) );
      entry.setUint32( 0 , 0x02014b50 , true );
      entry.setUint16( 4 , 20 , true );
      entry.setUint16( 6 , 20 , true );
      entry.setUint16( 8 , 0x0800 , true );
      entry.setUint16( 10 , 0 , true );
      entry.setUint16( 12 , when.time , true );
      entry.setUint16( 14 , when.date , true );
      entry.setUint32( 16 , crc , true );
      entry.setUint32( 20 , data.length , true );
      entry.setUint32( 24 , data.length , true );
      entry.setUint16( 28 , name.length , true );
      entry.setUint32( 42 , offset , true );
      central.push( new Uint8Array( entry.buffer ) , name );
      offset += 30 + name.length + data.length;
    } );
    const size = central.reduce( ( total , part ) => total + part.length , 0 );
    const end = new DataView( new ArrayBuffer( 22 ) );
    end.setUint32( 0 , 0x06054b50 , true );
    end.setUint16( 8 , files.length , true );
    end.setUint16( 10 , files.length , true );
    end.setUint32( 12 , size , true );
    end.setUint32( 16 , offset , true );
    return new Blob( [ ...parts , ...central , new Uint8Array( end.buffer ) ] , { type: "application/zip" } );
  },

  // A zip Blob -> { path: Uint8Array } for each file (folders left out).
  // Throws on what is not a zip.
  async read( blob ) {
    const buffer = await blob.arrayBuffer();
    const view = new DataView( buffer );
    let end = -1;
    for ( let i = buffer.byteLength - 22 ; i >= Math.max( 0 , buffer.byteLength - 22 - 65535 ) ; i-- ) {
      if ( view.getUint32( i , true ) === 0x06054b50 ) { end = i; break; }
    }
    if ( end < 0 ) throw new Error( "not a zip file" );
    const count = view.getUint16( end + 10 , true );
    let at = view.getUint32( end + 16 , true );
    const decoder = new TextDecoder();
    const out = {};
    for ( let n = 0 ; n < count ; n++ ) {
      if ( view.getUint32( at , true ) !== 0x02014b50 ) throw new Error( "not a zip file" );
      const method = view.getUint16( at + 10 , true );
      const packed = view.getUint32( at + 20 , true );
      const nameLength = view.getUint16( at + 28 , true );
      const extraLength = view.getUint16( at + 30 , true );
      const commentLength = view.getUint16( at + 32 , true );
      const local = view.getUint32( at + 42 , true );
      const path = decoder.decode( new Uint8Array( buffer , at + 46 , nameLength ) );
      at += 46 + nameLength + extraLength + commentLength;
      if ( path.endsWith( "/" ) ) continue;
      const start = local + 30 + view.getUint16( local + 26 , true ) + view.getUint16( local + 28 , true );
      const data = new Uint8Array( buffer , start , packed );
      if ( method === 0 ) out[ path ] = data;
      else if ( method === 8 ) {
        const stream = new Blob( [ data ] ).stream().pipeThrough( new DecompressionStream( "deflate-raw" ) );
        out[ path ] = new Uint8Array( await new Response( stream ).arrayBuffer() );
      }
    }
    return out;
  },
};
