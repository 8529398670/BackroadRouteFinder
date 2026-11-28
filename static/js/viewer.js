// The public view of a shared route: its public link, /v/<token>, opened by
// anyone, signed in or not (app/routes/saved_routes.py, "public links").
//
// It reads GET /api/public-routes/<token> once: the route as it was planned
// when its owner shared it (line, turn-by-turn steps, stops). It shows that
// on a map with a Navigate button, and Navigate drives it with the ordinary
// Navigate screen (navigate.js), moved in here. Nothing is planned, saved or
// exported: there is no planner behind this page, so driving off the line
// only says so (no rerouting).
const Viewer = {
  token: null,
  trip: null,
  map: null,

  // The token when the address is a public link.
  tokenInPath() {
    const match = /^\/v\/([A-Za-z0-9_-]{8,64})\/?$/.exec( window.location.pathname );
    return match ? match[ 1 ] : null;
  },

  async init( token ) {
    this.token = token;
    Dom.show( Dom.get( "public-view" ) , true );
    // The Navigate screen, without its planner-only controls.
    const panel = Dom.get( "navigate-panel" );
    panel.classList.add( "view-active" );
    Dom.show( panel , false );
    Dom.get( "public-view" ).appendChild( panel );
    Dom.show( Dom.get( "nav-reroute" ) , false );
    Dom.show( Dom.get( "nav-auto" ).closest( ".form-check" ) , false );
    Navigate.host = { drive: () => this.showDriving( true ) , leave: () => this.showDriving( false ) };
    // Its own trip slot: a reload mid-drive carries on, and never picks up
    // a trip from the signed-in app (or another link).
    Navigate.STORAGE = "navigate-public-" + token;
    Navigate.init();
    Dom.get( "pv-navigate" ).addEventListener( "click" , () => { if ( this.trip ) Navigate.start( this.trip ); } );

    let reply = null;
    try {
      reply = await Api.request( "/api/public-routes/" + encodeURIComponent( token ) );
    } catch ( error ) {
      const status = Dom.get( "pv-status" );
      status.classList.replace( "alert-info" , "alert-warning" );
      Dom.text( status , error.status === 404 ? I18n.get( "viewer.gone" ) : error.message );
      return;
    }
    this.trip = Object.assign( {} , reply.trip , { title: reply.name || reply.trip.title } );
    document.title = reply.name + " – " + I18n.get( "app.title" );
    Dom.show( Dom.get( "pv-status" ) , false );
    Dom.text( Dom.get( "pv-name" ) , reply.name );
    const route = this.trip.route;
    Dom.text( Dom.get( "pv-meta" ) , [
      reply.owner_name ? Format.fill( "viewer.shared_by" , { owner: reply.owner_name } ) : "",
      Format.distance( route.meters ),
      Format.duration( route.seconds ),
    ].filter( Boolean ).join( " · " ) );
    if ( Navigate.active ) this.showDriving( true );
    else this.showDriving( false );
  },

  showDriving( driving ) {
    Dom.show( Dom.get( "pv-overview" ) , !driving && Boolean( this.trip ) );
    Dom.show( Dom.get( "navigate-panel" ) , driving );
    if ( driving ) {
      if ( Navigate.active ) Navigate.show();
      return;
    }
    this.drawMap();
  },

  drawMap() {
    if ( !this.trip ) return;
    if ( !this.map ) {
      this.map = L.map( "pv-map" , { zoomControl: true , attributionControl: true , rotateControl: false } );
      L.tileLayer( "https://tile.openstreetmap.org/{z}/{x}/{y}.png" , {
        maxZoom: 19,
        // OpenStreetMap's tile servers block requests with no Referer (see map.js).
        referrerPolicy: "strict-origin-when-cross-origin",
        attribution: I18n.get( "plan.map_attribution" ),
      } ).addTo( this.map );
      const styles = getComputedStyle( document.documentElement );
      const back = styles.getPropertyValue( "--plan-back" ).trim() || "#198754";
      const main = styles.getPropertyValue( "--plan-main" ).trim() || "#dc3545";
      const runs = this.trip.route.runs;
      runs.forEach( ( run ) => L.polyline( run.coords , { color: "#ffffff" , weight: 9 , opacity: 0.9 , interactive: false } ).addTo( this.map ) );
      runs.forEach( ( run ) => L.polyline( run.coords , { color: run.main ? main : back , weight: 5 , opacity: 0.95 , interactive: false } ).addTo( this.map ) );
      const line = Geo.flatten( runs );
      const pin = ( point , label , kind ) => L.marker( point , {
        keyboard: false , interactive: false,
        icon: L.divIcon( { className: "plan-pin " + kind , html: "<span>" + label + "</span>" , iconSize: [ 28 , 28 ] , iconAnchor: [ 14 , 14 ] } ),
      } ).addTo( this.map );
      if ( line.length ) pin( line[ 0 ] , "A" , "plan-pin-start" );
      const stops = this.trip.stops || [];
      stops.slice( 1 , -1 ).forEach( ( stop , index ) => pin( [ stop.lat , stop.lon ] , String( index + 1 ) , "plan-pin-via" ) );
      if ( line.length > 1 ) pin( line[ line.length - 1 ] , "B" , "plan-pin-end" );
      this.bounds = line.length > 1 ? L.latLngBounds( line ) : null;
      window.addEventListener( "resize" , () => this.map.invalidateSize() );
    }
    window.setTimeout( () => {
      this.map.invalidateSize();
      if ( this.bounds ) this.map.fitBounds( this.bounds , { padding: [ 30 , 30 ] , maxZoom: 16 } );
    } , 0 );
  },
};
