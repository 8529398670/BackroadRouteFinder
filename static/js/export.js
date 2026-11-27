// A planned route as a file for phones, GPS units and other map apps, built
// here in the browser. Every format gets the same line (the route's runs,
// joined, repeats dropped) and the stops.
//
// GPX track is the default on purpose: a route (GPX rte, ITN, a Google Maps
// link) is only a list of points the device plans between, and most devices
// would put you back on the main roads. Where a format can only carry a
// route, it gets shaping points close together so the device has little
// room to choose.
const Export = {
  FORMATS: [
    { id: "gpx" , ext: "gpx" , type: "application/gpx+xml" },
    { id: "gpx_route" , ext: "gpx" , type: "application/gpx+xml" },
    { id: "kml" , ext: "kml" , type: "application/vnd.google-earth.kml+xml" },
    { id: "geojson" , ext: "geojson" , type: "application/geo+json" },
    { id: "tcx" , ext: "tcx" , type: "application/vnd.garmin.tcx+xml" },
    { id: "itn" , ext: "itn" , type: "text/plain" },
    { id: "csv" , ext: "csv" , type: "text/csv" },
    { id: "google" , link: true },
  ],
  SHAPING_METRES: 1000,
  ITN_MAX_POINTS: 255,
  GOOGLE_MAX_WAYPOINTS: 9,

  escape( text ) {
    return String( text ).replace( /&/g , "&amp;" ).replace( /</g , "&lt;" ).replace( />/g , "&gt;" ).replace( /"/g , "&quot;" );
  },

  // The route's line as [lat, lon] pairs, without repeats.
  line( route ) {
    const out = [];
    let last = null;
    route.runs.forEach( ( run ) => {
      run.coords.forEach( ( point ) => {
        const key = point[ 0 ] + "," + point[ 1 ];
        if ( key === last ) return;
        last = key;
        out.push( [ Number( point[ 0 ] ) , Number( point[ 1 ] ) ] );
      } );
    } );
    return out;
  },

  metres( a , b ) {
    const rad = Math.PI / 180;
    const dLat = ( b[ 0 ] - a[ 0 ] ) * rad;
    const dLon = ( b[ 1 ] - a[ 1 ] ) * rad;
    const h = Math.sin( dLat / 2 ) ** 2 + Math.cos( a[ 0 ] * rad ) * Math.cos( b[ 0 ] * rad ) * Math.sin( dLon / 2 ) ** 2;
    return 2 * 6371008.8 * Math.asin( Math.min( 1 , Math.sqrt( h ) ) );
  },

  // Distance from the start, in metres, at each point of the line.
  along( points ) {
    const out = [ 0 ];
    for ( let i = 1 ; i < points.length ; i++ ) out.push( out[ i - 1 ] + this.metres( points[ i - 1 ] , points[ i ] ) );
    return out;
  },

  // Points of the line at least `step` metres apart, always with the first and last.
  thin( points , step ) {
    if ( points.length < 3 ) return points.slice();
    const out = [ points[ 0 ] ];
    let gone = 0;
    for ( let i = 1 ; i < points.length - 1 ; i++ ) {
      gone += this.metres( points[ i - 1 ] , points[ i ] );
      if ( gone >= step ) { out.push( points[ i ] ); gone = 0; }
    }
    out.push( points[ points.length - 1 ] );
    return out;
  },

  // At most `count` points, evenly spread along the line.
  sample( points , count ) {
    if ( points.length <= count ) return points.slice();
    const out = [];
    for ( let i = 0 ; i < count ; i++ ) out.push( points[ Math.round( i * ( points.length - 1 ) / ( count - 1 ) ) ] );
    return out;
  },

  fixed( n ) {
    return Number( n ).toFixed( 6 );
  },

  gpx( name , stops , route ) {
    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="Backroad Route Finder" xmlns="http://www.topografix.com/GPX/1/1">',
      "  <metadata><name>" + this.escape( name ) + "</name></metadata>",
    ];
    stops.forEach( ( stop ) => {
      lines.push( '  <wpt lat="' + this.fixed( stop.lat ) + '" lon="' + this.fixed( stop.lon ) + '"><name>' + this.escape( stop.label || "" ) + "</name></wpt>" );
    } );
    lines.push( "  <trk><name>" + this.escape( name ) + "</name><trkseg>" );
    this.line( route ).forEach( ( p ) => lines.push( '    <trkpt lat="' + p[ 0 ] + '" lon="' + p[ 1 ] + '"></trkpt>' ) );
    lines.push( "  </trkseg></trk>" , "</gpx>" , "" );
    return lines.join( "\n" );
  },

  gpx_route( name , stops , route ) {
    const points = this.thin( this.line( route ) , this.SHAPING_METRES );
    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="Backroad Route Finder" xmlns="http://www.topografix.com/GPX/1/1">',
      "  <metadata><name>" + this.escape( name ) + "</name></metadata>",
      "  <rte><name>" + this.escape( name ) + "</name>",
    ];
    points.forEach( ( p , i ) => {
      const label = i === 0 ? ( stops[ 0 ] && stops[ 0 ].label ) || "Start"
        : i === points.length - 1 ? ( stops[ stops.length - 1 ] && stops[ stops.length - 1 ].label ) || "End" : "Shaping " + i;
      lines.push( '    <rtept lat="' + this.fixed( p[ 0 ] ) + '" lon="' + this.fixed( p[ 1 ] ) + '"><name>' + this.escape( label ) + "</name></rtept>" );
    } );
    lines.push( "  </rte>" , "</gpx>" , "" );
    return lines.join( "\n" );
  },

  kml( name , stops , route ) {
    const coords = this.line( route ).map( ( p ) => p[ 1 ] + "," + p[ 0 ] + ",0" ).join( " " );
    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
      "  <name>" + this.escape( name ) + "</name>",
      '  <Style id="route"><LineStyle><color>ff2a6bd6</color><width>5</width></LineStyle></Style>',
    ];
    stops.forEach( ( stop ) => {
      lines.push( "  <Placemark><name>" + this.escape( stop.label || "" ) + "</name><Point><coordinates>" + this.fixed( stop.lon ) + "," + this.fixed( stop.lat ) + ",0</coordinates></Point></Placemark>" );
    } );
    lines.push( "  <Placemark><name>" + this.escape( name ) + "</name><styleUrl>#route</styleUrl><LineString><tessellate>1</tessellate><coordinates>" + coords + "</coordinates></LineString></Placemark>" );
    lines.push( "</Document></kml>" , "" );
    return lines.join( "\n" );
  },

  geojson( name , stops , route ) {
    const features = [ {
      type: "Feature",
      properties: { name: name , kind: "route" , seconds: Math.round( route.seconds || 0 ) , meters: Math.round( route.meters || 0 ) },
      geometry: { type: "LineString" , coordinates: this.line( route ).map( ( p ) => [ p[ 1 ] , p[ 0 ] ] ) },
    } ];
    stops.forEach( ( stop , index ) => features.push( {
      type: "Feature",
      properties: { name: stop.label || "" , kind: "stop" , index: index },
      geometry: { type: "Point" , coordinates: [ stop.lon , stop.lat ] },
    } ) );
    return JSON.stringify( { type: "FeatureCollection" , features: features } , null , 1 ) + "\n";
  },

  // A Garmin course: the track with times spread by distance (Garmin wants
  // times; these are the planned pace, not a recording), and the stops as
  // course points.
  tcx( name , stops , route ) {
    const points = this.line( route );
    const along = this.along( points );
    const total = along[ along.length - 1 ] || 1;
    const seconds = route.seconds || total / 15;
    const start = Date.UTC( 2000 , 0 , 1 );
    const when = ( metres ) => new Date( start + 1000 * seconds * metres / total ).toISOString();
    const short = String( name ).slice( 0 , 15 );
    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">',
      "  <Courses><Course><Name>" + this.escape( short ) + "</Name>",
      "    <Lap><TotalTimeSeconds>" + Math.round( seconds ) + "</TotalTimeSeconds><DistanceMeters>" + total.toFixed( 1 ) + "</DistanceMeters>" +
        '<BeginPosition><LatitudeDegrees>' + points[ 0 ][ 0 ] + "</LatitudeDegrees><LongitudeDegrees>" + points[ 0 ][ 1 ] + "</LongitudeDegrees></BeginPosition>" +
        "<EndPosition><LatitudeDegrees>" + points[ points.length - 1 ][ 0 ] + "</LatitudeDegrees><LongitudeDegrees>" + points[ points.length - 1 ][ 1 ] + "</LongitudeDegrees></EndPosition>" +
        "<Intensity>Active</Intensity></Lap>",
      "    <Track>",
    ];
    points.forEach( ( p , i ) => {
      lines.push( "      <Trackpoint><Time>" + when( along[ i ] ) + "</Time><Position><LatitudeDegrees>" + p[ 0 ] + "</LatitudeDegrees><LongitudeDegrees>" + p[ 1 ] +
        "</LongitudeDegrees></Position><DistanceMeters>" + along[ i ].toFixed( 1 ) + "</DistanceMeters></Trackpoint>" );
    } );
    lines.push( "    </Track>" );
    stops.forEach( ( stop , index ) => {
      // The stop's place along the route: its nearest track point.
      let best = 0;
      let bestD = Infinity;
      points.forEach( ( p , i ) => {
        const d = this.metres( p , [ stop.lat , stop.lon ] );
        if ( d < bestD ) { bestD = d; best = i; }
      } );
      lines.push( "    <CoursePoint><Name>" + this.escape( String( stop.label || "Stop " + ( index + 1 ) ).slice( 0 , 10 ) ) + "</Name><Time>" + when( along[ best ] ) +
        "</Time><Position><LatitudeDegrees>" + this.fixed( stop.lat ) + "</LatitudeDegrees><LongitudeDegrees>" + this.fixed( stop.lon ) +
        "</LongitudeDegrees></Position><PointType>Generic</PointType></CoursePoint>" );
    } );
    lines.push( "  </Course></Courses>" , "</TrainingCenterDatabase>" , "" );
    return lines.join( "\n" );
  },

  // TomTom itinerary: lon|lat|name|type| in 1e-5 degrees; type 4 is the
  // departure, 2 the destination, 1 a waypoint (0 would be a hidden one, but
  // older units ignore those). Capped at the device's limit.
  itn( name , stops , route ) {
    const step = this.SHAPING_METRES;
    let points = this.thin( this.line( route ) , step );
    if ( points.length > this.ITN_MAX_POINTS ) points = this.sample( points , this.ITN_MAX_POINTS );
    const clean = ( text ) => String( text ).replace( /[|\r\n]+/g , " " ).trim();
    return points.map( ( p , i ) => {
      const last = i === points.length - 1;
      const label = i === 0 ? ( stops[ 0 ] && stops[ 0 ].label ) || "Start"
        : last ? ( stops[ stops.length - 1 ] && stops[ stops.length - 1 ].label ) || "End" : name + " " + i;
      return Math.round( p[ 1 ] * 1e5 ) + "|" + Math.round( p[ 0 ] * 1e5 ) + "|" + clean( label ) + "|" + ( i === 0 ? 4 : last ? 2 : 1 ) + "|";
    } ).join( "\r\n" ) + "\r\n";
  },

  csv( name , stops , route ) {
    const points = this.line( route );
    const along = this.along( points );
    return [ "lat,lon,km" ].concat( points.map( ( p , i ) => p[ 0 ] + "," + p[ 1 ] + "," + ( along[ i ] / 1000 ).toFixed( 3 ) ) ).join( "\n" ) + "\n";
  },

  // A Google Maps directions link: start, end, and up to nine points along
  // the line between. Google plans its own way between them.
  google( name , stops , route ) {
    const points = this.line( route );
    const middle = this.sample( points , this.GOOGLE_MAX_WAYPOINTS + 2 ).slice( 1 , -1 );
    const at = ( p ) => p[ 0 ].toFixed( 6 ) + "," + p[ 1 ].toFixed( 6 );
    const params = new URLSearchParams( {
      api: "1",
      origin: at( points[ 0 ] ),
      destination: at( points[ points.length - 1 ] ),
      travelmode: "driving",
    } );
    if ( middle.length ) params.set( "waypoints" , middle.map( at ).join( "|" ) );
    return "https://www.google.com/maps/dir/?" + params.toString();
  },

  fileName( name ) {
    return name.replace( /[^A-Za-z0-9 _-]+/g , "" ).trim().replace( /\s+/g , "-" ) || "route";
  },

  run( formatId , name , stops , route ) {
    const format = this.FORMATS.find( ( f ) => f.id === formatId );
    if ( !format || !route ) return;
    const made = this[ format.id ]( name , stops , route );
    if ( format.link ) {
      window.open( made , "_blank" , "noopener" );
      return;
    }
    Dom.download( this.fileName( name ) + "." + format.ext , made , format.type );
  },
};
