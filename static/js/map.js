// The planner's map: Leaflet (vendored) over OpenStreetMap's tiles. It draws
// the stops as numbered pins and the routes as lines, and tells the planner
// about clicks and drags. It keeps no route state of its own: planner.js
// hands it everything to show each time.
//
// Pins are Bootstrap-styled divs rather than Leaflet's image markers, so no
// image files are needed and they follow the theme's colors.
const RouteMap = {
  map: null,
  pins: null,
  lines: null,
  ring: null,
  fitted: false,

  init( elementId , handlers ) {
    this.map = L.map( elementId , { zoomControl: true , attributionControl: true } );
    L.tileLayer( "https://tile.openstreetmap.org/{z}/{x}/{y}.png" , {
      maxZoom: 19,
      // The app's own pages send no Referer off-site (Referrer-Policy:
      // same-origin), but OpenStreetMap's tile servers block requests that
      // carry none. The tiles alone send the site's origin, never a path.
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: I18n.get( "plan.map_attribution" ),
    } ).addTo( this.map );
    this.ring = L.layerGroup().addTo( this.map );
    this.lines = L.layerGroup().addTo( this.map );
    this.pins = L.layerGroup().addTo( this.map );
    this.map.on( "click" , ( event ) => handlers.click( event.latlng.lat , event.latlng.lng ) );
    this.handlers = handlers;
    // Ohio until the map's own area is known.
    this.map.setView( [ 40.3 , -82.8 ] , 7 );
  },

  // The map's area from GET /api/routing/status: [south, west, north, east].
  // A hidden screen has no size, so the area waits until the map is shown.
  showArea( bbox ) {
    if ( !bbox || bbox.length !== 4 || this.fitted ) return;
    this.pendingArea = bbox;
    this.resize();
  },

  // A tab or a hidden screen gives Leaflet no size; call when it is shown.
  resize() {
    if ( !this.map ) return;
    this.map.invalidateSize();
    const size = this.map.getSize();
    if ( this.pendingArea && size.x > 0 && size.y > 0 ) {
      const bbox = this.pendingArea;
      this.pendingArea = null;
      if ( !this.fitted ) this.map.fitBounds( [ [ bbox[ 0 ] , bbox[ 1 ] ] , [ bbox[ 2 ] , bbox[ 3 ] ] ] );
    }
  },

  pinLabel( index , count ) {
    if ( index === 0 ) return "A";
    if ( index === count - 1 ) return "B";
    return String( index );
  },

  // kindOf( index ) -> { kind , label } for pins other than A, 1, 2... B (a
  // loop's center and start); without it, the stops of a trip.
  drawStops( stops , kindOf ) {
    const count = stops.length;
    const layers = [];
    stops.forEach( ( stop , index ) => {
      if ( !stop ) return;
      const own = kindOf ? kindOf( index ) : null;
      const kind = own ? own.kind : index === 0 ? "start" : index === count - 1 ? "end" : "via";
      const label = own ? own.label : this.pinLabel( index , count );
      const marker = L.marker( [ stop.lat , stop.lon ] , {
        draggable: true,
        keyboard: true,
        title: stop.label,
        icon: L.divIcon( {
          className: "plan-pin plan-pin-" + kind,
          html: "<span>" + label + "</span>",
          iconSize: [ 28 , 28 ],
          iconAnchor: [ 14 , 14 ],
        } ),
      } );
      marker.on( "dragend" , () => {
        const at = marker.getLatLng();
        this.handlers.drag( index , at.lat , at.lng );
      } );
      layers.push( marker );
    } );
    this.pins.clearLayers();
    layers.forEach( ( layer ) => this.pins.addLayer( layer ) );
  },

  // routes as the API answers them; the first is the one to follow, the
  // second (fastest, for comparison) is drawn faint and dashed underneath.
  drawRoutes( routes ) {
    const layers = [];
    const styles = getComputedStyle( document.documentElement );
    const back = styles.getPropertyValue( "--plan-back" ).trim() || "#198754";
    const main = styles.getPropertyValue( "--plan-main" ).trim() || "#dc3545";
    const fastest = styles.getPropertyValue( "--plan-fastest" ).trim() || "#6c757d";
    routes.slice( 1 ).forEach( ( route ) => {
      route.runs.forEach( ( run ) => {
        layers.push( L.polyline( run.coords , { color: fastest , weight: 4 , opacity: 0.6 , dashArray: "6 8" , interactive: false } ) );
      } );
    } );
    if ( routes.length ) {
      routes[ 0 ].runs.forEach( ( run ) => {
        layers.push( L.polyline( run.coords , { color: "#ffffff" , weight: 9 , opacity: 0.9 , interactive: false } ) );
      } );
      routes[ 0 ].runs.forEach( ( run ) => {
        layers.push( L.polyline( run.coords , { color: run.main ? main : back , weight: 5 , opacity: 0.95 , interactive: false } ) );
      } );
    }
    this.lines.clearLayers();
    this.ring.clearLayers();
    layers.forEach( ( layer ) => this.lines.addLayer( layer ) );
  },

  fitTo( routes , stops ) {
    const points = [];
    ( routes || [] ).forEach( ( route ) => route.runs.forEach( ( run ) => run.coords.forEach( ( point ) => points.push( point ) ) ) );
    ( stops || [] ).forEach( ( stop ) => { if ( stop ) points.push( [ stop.lat , stop.lon ] ); } );
    if ( points.length === 0 ) return;
    this.fitted = true;
    // One stop: only pan to it, so the other end can still be clicked.
    if ( points.length === 1 ) {
      this.map.panTo( points[ 0 ] );
      return;
    }
    this.map.fitBounds( L.latLngBounds( points ) , { padding: [ 30 , 30 ] , maxZoom: 15 } );
  },

  // A loop's target: a dashed circle of radius metres round the center (a
  // radius in miles), or a dashed ring through the ring points (in minutes,
  // where the ring follows the roads), with a dot at each ring point.
  drawRing( center , ring , metres ) {
    const color = getComputedStyle( document.documentElement ).getPropertyValue( "--plan-ring" ).trim() || "#6f42c1";
    const style = { color: color , weight: 2 , opacity: 0.7 , dashArray: "4 6" , fill: false , interactive: false };
    const layers = [];
    if ( metres && center ) layers.push( L.circle( [ center.lat , center.lon ] , Object.assign( { radius: metres } , style ) ) );
    else if ( ring.length > 2 ) layers.push( L.polygon( ring.map( ( point ) => [ point.lat , point.lon ] ) , style ) );
    ring.forEach( ( point ) => layers.push( L.circleMarker( [ point.lat , point.lon ] , {
      radius: 4 , color: color , weight: 2 , fillColor: "#ffffff" , fillOpacity: 1 , interactive: false,
    } ) ) );
    this.ring.clearLayers();
    layers.forEach( ( layer ) => this.ring.addLayer( layer ) );
  },

  clearRoutes() {
    this.lines.clearLayers();
    this.ring.clearLayers();
  },
};
