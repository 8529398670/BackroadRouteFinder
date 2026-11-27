// The planner's map: Leaflet (vendored) over OpenStreetMap's tiles. It draws
// the stops as numbered pins and the routes as lines, and tells the planner
// about clicks and drags. It keeps no route state of its own: planner.js
// hands it everything to show each time.
//
// Pins are Bootstrap-styled divs rather than Leaflet's image markers, so no
// image files are needed and they follow the theme's colors.
//
// The chosen route can be pulled into shape: hovering it shows a small
// handle on the line (a tap shows it on a phone), and dragging the handle
// tells the planner where the line was grabbed and where it was dropped
// (handlers.nudge). The nudges it makes are small dots of their own
// (drawShape), and a locked route's two ends get a padlock pin (drawLocks).
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
    this.edit = L.layerGroup().addTo( this.map );
    this.shape = L.layerGroup().addTo( this.map );
    this.locks = L.layerGroup().addTo( this.map );
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
  drawRoutes( routes , onPick ) {
    const layers = [];
    const styles = getComputedStyle( document.documentElement );
    const back = styles.getPropertyValue( "--plan-back" ).trim() || "#198754";
    const main = styles.getPropertyValue( "--plan-main" ).trim() || "#dc3545";
    const fastest = styles.getPropertyValue( "--plan-fastest" ).trim() || "#6c757d";
    routes.slice( 1 ).forEach( ( route ) => {
      route.runs.forEach( ( run ) => {
        const line = L.polyline( run.coords , { color: fastest , weight: 4 , opacity: 0.6 , dashArray: "6 8" , interactive: false } );
        layers.push( line );
        // A wide invisible line over each other route, to click it.
        if ( onPick && !route.compare ) {
          const hit = L.polyline( run.coords , { color: "#000000" , weight: 16 , opacity: 0 , interactive: true , bubblingMouseEvents: false } );
          hit.on( "click" , () => onPick( route ) );
          hit.on( "mouseover" , () => this.highlight( route ) );
          hit.on( "mouseout" , () => this.highlight( null ) );
          layers.push( hit );
        }
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
    if ( this.glow ) this.glow.clearLayers();
    layers.forEach( ( layer ) => this.lines.addLayer( layer ) );
  },

  // Another route shown boldly for a moment (hovering its row or its line);
  // null takes it away.
  highlight( route ) {
    if ( !this.glow ) this.glow = L.layerGroup().addTo( this.map );
    this.glow.clearLayers();
    if ( !route ) return;
    const color = getComputedStyle( document.documentElement ).getPropertyValue( "--bs-primary" ).trim() || "#0d6efd";
    route.runs.forEach( ( run ) => {
      this.glow.addLayer( L.polyline( run.coords , { color: color , weight: 6 , opacity: 0.85 , interactive: false } ) );
    } );
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
    if ( this.glow ) this.glow.clearLayers();
    this.drawEdit( null );
    this.drawShape( [] );
    this.drawLocks( [] );
  },

  // The part of the chosen route that can be grabbed, as [lat, lon] pairs
  // (null: none). A wide invisible line over it finds the nearest point.
  drawEdit( coords ) {
    this.edit.clearLayers();
    this.editHit = null;
    this.hideGhost( true );
    if ( !coords || coords.length < 2 ) return;
    const hit = L.polyline( coords , { color: "#000000" , weight: 18 , opacity: 0 , interactive: true , bubblingMouseEvents: false } );
    hit.on( "mousemove" , ( event ) => this.showGhost( event.latlng ) );
    hit.on( "mouseout" , () => this.hideGhost( false ) );
    hit.on( "click" , ( event ) => {
      const at = this.onLine( event.latlng );
      if ( !this.handlers.linePick( at.lat , at.lng ) ) this.showGhost( event.latlng );
    } );
    this.edit.addLayer( hit );
    this.editHit = hit;
  },

  // The point on the grabbable line nearest to latlng.
  onLine( latlng ) {
    if ( !this.editHit ) return latlng;
    return this.map.layerPointToLatLng( this.editHit.closestLayerPoint( this.map.latLngToLayerPoint( latlng ) ) );
  },

  showGhost( latlng ) {
    if ( !this.editHit ) return;
    window.clearTimeout( this.ghostTimer );
    if ( !this.ghost ) {
      const ghost = L.marker( latlng , {
        draggable: true,
        keyboard: false,
        title: I18n.get( "plan.nudge_handle" ),
        icon: L.divIcon( { className: "plan-pin plan-pin-ghost" , iconSize: [ 16 , 16 ] , iconAnchor: [ 8 , 8 ] } ),
      } );
      ghost.on( "mouseover" , () => window.clearTimeout( this.ghostTimer ) );
      ghost.on( "mouseout" , () => this.hideGhost( false ) );
      ghost.on( "dragstart" , () => { this.ghostFrom = ghost.getLatLng(); this.ghostDragging = true; } );
      ghost.on( "dragend" , () => {
        const from = this.ghostFrom;
        const to = ghost.getLatLng();
        this.ghostDragging = false;
        this.hideGhost( true );
        this.handlers.nudge( from.lat , from.lng , to.lat , to.lng );
      } );
      ghost.on( "click" , () => {
        const at = ghost.getLatLng();
        this.handlers.linePick( at.lat , at.lng );
      } );
      this.ghost = ghost;
    }
    if ( this.ghostDragging ) return;
    this.ghost.setLatLng( this.onLine( latlng ) );
    if ( !this.map.hasLayer( this.ghost ) ) this.ghost.addTo( this.map );
  },

  // now: at once (the line changed); otherwise a moment after the mouse
  // leaves, so it can move from the line onto the handle.
  hideGhost( now ) {
    window.clearTimeout( this.ghostTimer );
    if ( !this.ghost || this.ghostDragging ) return;
    const hide = () => { if ( this.ghost && this.map.hasLayer( this.ghost ) ) this.map.removeLayer( this.ghost ); };
    if ( now ) hide();
    else this.ghostTimer = window.setTimeout( hide , 700 );
  },

  // The nudges: small dots to drag (handlers.moveShape) or click away
  // (handlers.removeShape).
  drawShape( points ) {
    this.shape.clearLayers();
    points.forEach( ( point , index ) => {
      const dot = L.marker( [ point.lat , point.lon ] , {
        draggable: true,
        keyboard: true,
        title: I18n.get( "plan.nudge_dot" ),
        icon: L.divIcon( { className: "plan-pin plan-pin-shape" , iconSize: [ 14 , 14 ] , iconAnchor: [ 7 , 7 ] } ),
      } );
      dot.on( "dragend" , () => {
        const at = dot.getLatLng();
        this.handlers.moveShape( index , at.lat , at.lng );
      } );
      dot.on( "click" , () => this.handlers.removeShape( index ) );
      this.shape.addLayer( dot );
    } );
  },

  // A locked route's ends (and the first one while the second is picked).
  drawLocks( points ) {
    this.locks.clearLayers();
    points.forEach( ( point ) => {
      this.locks.addLayer( L.marker( [ point[ 0 ] , point[ 1 ] ] , {
        interactive: false,
        keyboard: false,
        icon: L.divIcon( {
          className: "plan-pin plan-pin-lock",
          html: "<i class=\"bi bi-lock-fill\" aria-hidden=\"true\"></i>",
          iconSize: [ 24 , 24 ],
          iconAnchor: [ 12 , 12 ],
        } ),
      } ) );
    } );
  },
};
