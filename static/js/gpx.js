// A planned route as a GPX file, built here in the browser, for phones, GPS
// units and other map apps. The stops become waypoints and the line a track;
// a route (rte) is left out on purpose, since most devices would recalculate
// a route between its points and put you back on the main roads.
const Gpx = {
  escape( text ) {
    return String( text ).replace( /&/g , "&amp;" ).replace( /</g , "&lt;" ).replace( />/g , "&gt;" ).replace( /"/g , "&quot;" );
  },

  build( name , stops , route ) {
    const lines = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<gpx version="1.1" creator="Backroad Route Finder" xmlns="http://www.topografix.com/GPX/1/1">',
      "  <metadata><name>" + this.escape( name ) + "</name></metadata>",
    ];
    stops.forEach( ( stop ) => {
      lines.push( '  <wpt lat="' + stop.lat.toFixed( 6 ) + '" lon="' + stop.lon.toFixed( 6 ) + '"><name>' + this.escape( stop.label ) + "</name></wpt>" );
    } );
    lines.push( "  <trk><name>" + this.escape( name ) + "</name><trkseg>" );
    let last = null;
    route.runs.forEach( ( run ) => {
      run.coords.forEach( ( point ) => {
        const key = point[ 0 ] + "," + point[ 1 ];
        if ( key === last ) return;
        last = key;
        lines.push( '    <trkpt lat="' + point[ 0 ] + '" lon="' + point[ 1 ] + '"></trkpt>' );
      } );
    } );
    lines.push( "  </trkseg></trk>" , "</gpx>" , "" );
    return lines.join( "\n" );
  },

  download( name , stops , route ) {
    const file = name.replace( /[^A-Za-z0-9 _-]+/g , "" ).trim().replace( /\s+/g , "-" ) || "route";
    Dom.download( file + ".gpx" , this.build( name , stops , route ) );
  },
};
