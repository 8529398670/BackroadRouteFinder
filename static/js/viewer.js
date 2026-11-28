// The public view of a shared route: its public link, /v/<token>, opened by
// anyone, signed in or not (app/routes/saved_routes.py, "public links").
//
// It reads GET /api/public-routes/<token> once: the route as it was planned
// when its owner shared it (line, turn-by-turn steps, stops, and the other
// routes offered with it), and drives it on the ordinary Navigate screen
// (navigate.js), moved in here. Nothing is planned, saved or exported: there
// is no planner behind this page, so driving off the line only says so (no
// rerouting), and there is nowhere to go back to, so no End button.
const Viewer = {
  // The token when the address is a public link.
  tokenInPath() {
    const match = /^\/v\/([A-Za-z0-9_-]{8,64})\/?$/.exec( window.location.pathname );
    return match ? match[ 1 ] : null;
  },

  async init( token ) {
    Dom.show( Dom.get( "public-view" ) , true );
    const panel = Dom.get( "navigate-panel" );
    panel.classList.add( "view-active" );
    Dom.get( "public-view" ).appendChild( panel );
    Dom.show( Dom.get( "nav-reroute" ) , false );
    Dom.show( Dom.get( "nav-auto" ).closest( ".form-check" ) , false );
    Dom.show( Dom.get( "nav-stop" ) , false );
    Navigate.host = { drive: () => {} , leave: () => {} };
    // Its own trip slot, so it never picks up (or overwrites) a trip from
    // the signed-in app or another link.
    Navigate.STORAGE = "navigate-public-" + token;
    Navigate.init();

    let reply = null;
    try {
      reply = await Api.request( "/api/public-routes/" + encodeURIComponent( token ) );
    } catch ( error ) {
      const status = Dom.get( "pv-status" );
      status.classList.replace( "alert-info" , "alert-warning" );
      Dom.text( status , error.status === 404 ? I18n.get( "viewer.gone" ) : error.message );
      return;
    }
    document.title = reply.name + " – " + I18n.get( "app.title" );
    Dom.show( Dom.get( "pv-status" ) , false );
    // Always the copy just read, never one kept from an earlier visit: the
    // owner may have saved the link again since (a newer route, its other
    // routes). Where the driver is comes from GPS either way.
    Navigate.start( Object.assign( {} , reply.trip , { title: reply.name || reply.trip.title } ) );
  },
};
