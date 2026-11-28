// The device's own position (the browser's Geolocation API), for the
// planner's "my location" and the Navigate screen.
//
// One watch is shared: each screen that wants fixes calls start(who) and
// stop(who), and the watch runs while anyone still wants it. Every fix is a
// "gps" event on window (detail: { lat , lon , accuracy , heading , speed ,
// time }); a failure is a "gps-error" event (detail: { code , message }).
//
// The browser only gives a position to a secure page (https, or localhost),
// and only after the person allows it; the server's Permissions-Policy lets
// this page ask (app/core/middleware.py).
const Gps = {
  watchId: null,
  wanted: new Set(),
  last: null,
  error: null,

  supported() {
    return "geolocation" in navigator;
  },

  start( who ) {
    this.wanted.add( who );
    if ( this.watchId !== null ) {
      if ( this.last ) window.setTimeout( () => this.emit( "gps" , this.last ) , 0 );
      return;
    }
    if ( !window.isSecureContext ) return this.fail( { code: "insecure" } );
    if ( !this.supported() ) return this.fail( { code: "unsupported" } );
    this.watchId = navigator.geolocation.watchPosition(
      ( position ) => this.fix( position ),
      ( error ) => this.fail( error ),
      { enableHighAccuracy: true , maximumAge: 1000 , timeout: 30000 },
    );
  },

  stop( who ) {
    this.wanted.delete( who );
    if ( this.wanted.size || this.watchId === null ) return;
    navigator.geolocation.clearWatch( this.watchId );
    this.watchId = null;
  },

  // One fix, for a single question ("where am I?"): the next one the watch
  // gives, or the last one if it is fresh.
  once() {
    if ( this.last && Date.now() - this.last.time < 10000 ) return Promise.resolve( this.last );
    return new Promise( ( resolve , reject ) => {
      const done = ( ok , detail ) => {
        window.removeEventListener( "gps" , onFix );
        window.removeEventListener( "gps-error" , onError );
        this.stop( "once" );
        if ( ok ) resolve( detail ); else reject( new Error( detail.message ) );
      };
      const onFix = ( event ) => done( true , event.detail );
      const onError = ( event ) => done( false , event.detail );
      window.addEventListener( "gps" , onFix );
      window.addEventListener( "gps-error" , onError );
      this.start( "once" );
    } );
  },

  fix( position ) {
    const c = position.coords;
    this.error = null;
    this.last = {
      lat: c.latitude,
      lon: c.longitude,
      accuracy: c.accuracy || 0,
      heading: typeof c.heading === "number" && !Number.isNaN( c.heading ) ? c.heading : null,
      speed: typeof c.speed === "number" && !Number.isNaN( c.speed ) ? c.speed : null,
      time: position.timestamp || Date.now(),
    };
    this.emit( "gps" , this.last );
  },

  // A refusal ends the watch (asking again would only be refused again); a
  // fix that is slow to come keeps it, and the next one clears the warning.
  fail( error ) {
    const codes = { 1: "denied" , 2: "unavailable" , 3: "timeout" };
    const code = codes[ error.code ] || error.code || "unavailable";
    this.error = { code: code , message: I18n.get( "gps." + code ) };
    if ( code !== "timeout" && this.watchId !== null ) {
      navigator.geolocation.clearWatch( this.watchId );
      this.watchId = null;
    }
    if ( code !== "timeout" ) this.wanted.clear();
    window.setTimeout( () => this.emit( "gps-error" , this.error ) , 0 );
  },

  emit( name , detail ) {
    window.dispatchEvent( new CustomEvent( name , { detail: detail } ) );
  },

  // The blue dot with its accuracy circle, on a Leaflet map. update(fix)
  // moves it; a heading turns its arrow. On a rotated map (leaflet-rotate,
  // the Navigate screen's heading-up) the icon stays upright on the screen,
  // so the arrow turns by the heading plus the map's (clockwise) bearing.
  dot( map , onClick ) {
    const layer = L.layerGroup().addTo( map );
    let circle = null;
    let marker = null;
    return {
      update( fix ) {
        const at = [ fix.lat , fix.lon ];
        const arrow = fix.heading !== null && fix.speed !== null && fix.speed > 0.5;
        const html = arrow
          ? "<span class=\"gps-dot-arrow\"><i class=\"bi bi-caret-up-fill\" aria-hidden=\"true\"></i></span>"
          : "";
        // The CSP (style-src 'self') drops a style="" written into the HTML, so
        // the arrow is turned through the DOM once the icon is on the map.
        const turn = () => {
          const span = arrow && marker && marker.getElement() ? marker.getElement().querySelector( ".gps-dot-arrow" ) : null;
          if ( span ) span.style.transform = "rotate(" + Math.round( fix.heading + ( map.getBearing ? map.getBearing() : 0 ) ) + "deg)";
        };
        if ( !marker ) {
          circle = L.circle( at , { radius: fix.accuracy , color: "#0d6efd" , weight: 1 , opacity: 0.4 , fillOpacity: 0.12 , interactive: false } );
          marker = L.marker( at , {
            keyboard: false,
            zIndexOffset: 2000,
            title: I18n.get( "gps.you" ),
            icon: L.divIcon( { className: "gps-dot" , html: html , iconSize: [ 20 , 20 ] , iconAnchor: [ 10 , 10 ] } ),
          } );
          if ( onClick ) marker.on( "click" , () => onClick( fix , marker ) );
          layer.addLayer( circle );
          layer.addLayer( marker );
          turn();
        } else {
          circle.setLatLng( at ).setRadius( fix.accuracy );
          marker.setLatLng( at );
          marker.setIcon( L.divIcon( { className: "gps-dot" , html: html , iconSize: [ 20 , 20 ] , iconAnchor: [ 10 , 10 ] } ) );
          turn();
          if ( onClick ) { marker.off( "click" ); marker.on( "click" , () => onClick( fix , marker ) ); }
        }
      },
      clear() {
        layer.clearLayers();
        circle = null;
        marker = null;
      },
      marker() {
        return marker;
      },
    };
  },
};

// Distances along a line of [lat, lon] pairs: where a point falls on it,
// how far along, and how far off. Flat earth, which is exact enough over
// the few hundred metres anything here is measured across.
const Geo = {
  metres( lat1 , lon1 , lat2 , lon2 ) {
    const x = ( lon2 - lon1 ) * Math.cos( ( ( lat1 + lat2 ) / 2 ) * Math.PI / 180 );
    return Math.hypot( x , lat2 - lat1 ) * 111195;
  },

  // Metres from the start at each point.
  cumulative( line ) {
    const out = [ 0 ];
    for ( let i = 1 ; i < line.length ; i++ ) out.push( out[ i - 1 ] + this.metres( line[ i - 1 ][ 0 ] , line[ i - 1 ][ 1 ] , line[ i ][ 0 ] , line[ i ][ 1 ] ) );
    return out;
  },

  // Where (lat, lon) meets the line between point lo and point hi: { along ,
  // off , lat , lon , index } with off its distance from the line.
  snap( line , cum , lat , lon , lo , hi ) {
    let best = null;
    const k = Math.cos( lat * Math.PI / 180 );
    for ( let i = Math.max( 0 , lo ) ; i < Math.min( hi , line.length - 1 ) ; i++ ) {
      const a = line[ i ];
      const b = line[ i + 1 ];
      const ax = a[ 1 ] * k , ay = a[ 0 ] , bx = b[ 1 ] * k , by = b[ 0 ] , px = lon * k , py = lat;
      const dx = bx - ax , dy = by - ay;
      const span = dx * dx + dy * dy;
      const t = span > 0 ? Math.max( 0 , Math.min( 1 , ( ( px - ax ) * dx + ( py - ay ) * dy ) / span ) ) : 0;
      const qlat = a[ 0 ] + ( b[ 0 ] - a[ 0 ] ) * t;
      const qlon = a[ 1 ] + ( b[ 1 ] - a[ 1 ] ) * t;
      const off = this.metres( lat , lon , qlat , qlon );
      if ( !best || off < best.off ) best = { off: off , lat: qlat , lon: qlon , index: i , along: cum[ i ] + ( cum[ i + 1 ] - cum[ i ] ) * t };
    }
    return best;
  },

  // The first point at least along metres from the start.
  indexAt( cum , along ) {
    let lo = 0 , hi = cum.length - 1;
    while ( lo < hi ) {
      const mid = ( lo + hi ) >> 1;
      if ( cum[ mid ] < along ) lo = mid + 1; else hi = mid;
    }
    return lo;
  },

  // The runs of a route as one line, the shared point where two meet kept once.
  flatten( runs ) {
    const out = [];
    ( runs || [] ).forEach( ( run ) => run.coords.forEach( ( point , index ) => {
      const last = out[ out.length - 1 ];
      if ( index === 0 && last && last[ 0 ] === point[ 0 ] && last[ 1 ] === point[ 1 ] ) return;
      out.push( point );
    } ) );
    return out;
  },
};
