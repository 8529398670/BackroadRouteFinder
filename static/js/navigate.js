// The Navigate screen: drives a route already planned, turn by turn, from
// the device's GPS (gps.js).
//
// A trip comes from the planner (Planner.navigation: the route exactly as
// shown, with the steps POST /api/route worked out for its line), from the
// planner's Navigate button or a saved route's. It is kept in sessionStorage,
// so a reload (or a newer build loading) carries on where it was.
//
// Each fix is put on the line, searching ahead of where the car last was so a
// route that comes back past itself does not jump. That gives the next step,
// the distance to it, and what is left. A car off the line for a few fixes in
// a row is off route: the rest of the trip, from where it is to the stops
// still ahead, is planned again (POST /api/route) and driven instead, at most
// once every REROUTE_MS. Turns are spoken (speechSynthesis) unless muted, and
// the screen is kept awake (Wake Lock) while driving.
const Navigate = {
  STORAGE: "navigate-trip",
  MUTE: "navigate-muted",
  // "north" (north always up) or "heading" (the way the car is going up).
  ORIENT: "navigate-orientation",
  // Below this speed (m/s) the GPS heading is noise; the line's own direction is used.
  HEADING_MIN_SPEED: 1.5,
  // Heading-up puts the car this share of the map's height below the middle,
  // so more of the road ahead shows.
  AHEAD_SHARE: 0.25,
  AUTO: "navigate-auto-reroute",
  // Off the line by more than this (plus the fix's own uncertainty, up to
  // OFF_SLACK_M) for OFF_FIXES fixes in a row is off route.
  OFF_M: 50,
  OFF_SLACK_M: 50,
  OFF_FIXES: 3,
  REROUTE_MS: 20000,
  ARRIVE_M: 30,
  // Spoken before a turn at these distances (when the step is longer), and at it.
  SAY_AT: [ 805 , 160 ],
  SAY_NOW_M: 30,
  WAYPOINTS_MAX: 10,

  trip: null,
  line: [],
  cum: [],
  along: 0,
  next: 0,
  offCount: 0,
  lastReroute: 0,
  rerouting: false,
  spoken: {},
  following: true,
  bearing: 0,
  arrived: false,
  active: false,
  wake: null,
  // The public view (viewer.js) when it is driving: { drive(), leave() }.
  // There is no planner or router behind it, so no rerouting.
  host: null,

  init() {
    this.map = null;
    Dom.get( "nav-stop" ).addEventListener( "click" , () => this.stop() );
    Dom.get( "nav-recenter" ).addEventListener( "click" , () => this.recenter() );
    Dom.get( "nav-reroute" ).addEventListener( "click" , () => this.reroute( true ) );
    const mute = Dom.get( "nav-mute" );
    mute.addEventListener( "click" , () => {
      this.setMuted( !this.muted() );
      if ( this.muted() && window.speechSynthesis ) window.speechSynthesis.cancel();
    } );
    const auto = Dom.get( "nav-auto" );
    auto.checked = window.localStorage.getItem( this.AUTO ) !== "off";
    auto.addEventListener( "change" , () => window.localStorage.setItem( this.AUTO , auto.checked ? "on" : "off" ) );
    this.renderMute();
    Dom.get( "nav-orient" ).addEventListener( "click" , () => this.setOrientation( this.orientation() === "heading" ? "north" : "heading" ) );
    this.renderOrientation();
    window.addEventListener( "gps" , ( event ) => { if ( this.active ) this.onFix( event.detail ); } );
    window.addEventListener( "gps-error" , ( event ) => { if ( this.active ) this.showGps( event.detail.message , true ); } );
    window.addEventListener( "nav" , ( event ) => {
      if ( event.detail.screen !== "navigate" ) return;
      this.show();
    } );
    window.addEventListener( "resize" , () => { if ( this.map ) this.map.invalidateSize(); } );
    document.addEventListener( "visibilitychange" , () => {
      if ( !document.hidden && this.active ) this.keepAwake();
    } );
    // A reload mid-drive would lose nothing, but would interrupt a voice
    // prompt and the wake lock; a newer build waits until the trip ends.
    Fresh.holdWhile( () => this.active );
    this.restore();
  },

  // --- the trip ------------------------------------------------------------------

  // From the planner: drive this (Planner.navigation's shape).
  start( trip ) {
    this.load( trip );
    this.save();
    if ( this.host ) {
      this.host.drive();
      this.show();
      return;
    }
    Nav.go( Nav.pathOf( "navigate" ) );
  },

  load( trip ) {
    this.trip = trip;
    this.line = Geo.flatten( trip.route.runs );
    this.cum = Geo.cumulative( this.line );
    this.along = 0;
    this.next = 1;
    this.offCount = 0;
    this.spoken = {};
    this.arrived = false;
    this.following = true;
    this.steps = this.stepsOf( trip.route );
    this.timeline();
    this.active = true;
    this.drawn = false;
  },

  save() {
    try {
      window.sessionStorage.setItem( this.STORAGE , JSON.stringify( this.trip ) );
    } catch ( error ) {
      // A huge line may not fit; the trip still runs, it just will not survive a reload.
    }
  },

  restore() {
    let trip = null;
    try {
      trip = JSON.parse( window.sessionStorage.getItem( this.STORAGE ) || "null" );
    } catch ( error ) {
      trip = null;
    }
    if ( trip && trip.route && trip.route.runs ) this.load( trip );
  },

  stop() {
    this.active = false;
    this.trip = null;
    window.sessionStorage.removeItem( this.STORAGE );
    Gps.stop( "navigate" );
    if ( window.speechSynthesis ) window.speechSynthesis.cancel();
    this.release();
    this.render();
    if ( this.host ) this.host.leave();
    else Nav.go( Nav.pathOf( "routes" ) );
  },

  // The route's own steps, or (a route planned before steps existed) just
  // its start and end.
  stepsOf( route ) {
    const total = this.cum[ this.cum.length - 1 ] || 0;
    const steps = ( route.steps || [] ).slice();
    if ( steps.length >= 2 ) {
      // The server measures along its own copy of the line; put each step on ours.
      const scale = steps[ steps.length - 1 ].along_m > 0 ? total / steps[ steps.length - 1 ].along_m : 1;
      return steps.map( ( step ) => Object.assign( {} , step , { along: step.along_m * scale } ) );
    }
    const first = this.line[ 0 ] || [ 0 , 0 ];
    const last = this.line[ this.line.length - 1 ] || first;
    return [
      { type: "depart" , modifier: "" , road: "" , lat: first[ 0 ] , lon: first[ 1 ] , along: 0 , meters: total , seconds: route.seconds },
      { type: "arrive" , modifier: "" , road: "" , lat: last[ 0 ] , lon: last[ 1 ] , along: total , meters: 0 , seconds: 0 },
    ];
  },

  // Seconds still to drive from each step, so the time left at any point is
  // the rest of its step's share plus every step after it.
  timeline() {
    const after = new Array( this.steps.length ).fill( 0 );
    for ( let i = this.steps.length - 2 ; i >= 0 ; i-- ) after[ i ] = after[ i + 1 ] + ( this.steps[ i ].seconds || 0 );
    this.secondsAfter = after;
  },

  secondsLeft( along ) {
    const steps = this.steps;
    let i = 0;
    while ( i < steps.length - 1 && steps[ i + 1 ].along <= along ) i++;
    if ( i >= steps.length - 1 ) return 0;
    const span = steps[ i + 1 ].along - steps[ i ].along;
    const done = span > 0 ? ( along - steps[ i ].along ) / span : 0;
    return this.secondsAfter[ i + 1 ] + ( steps[ i ].seconds || 0 ) * ( 1 - Math.min( 1 , Math.max( 0 , done ) ) );
  },

  // --- the screen ------------------------------------------------------------------

  // Navigation is started from a route (saved, or the planner's); with no
  // trip there is nothing to show here, so go to the saved routes.
  show() {
    this.render();
    if ( !this.active ) {
      if ( this.host ) this.host.leave();
      else Nav.go( Nav.pathOf( "routes" ) );
      return;
    }
    this.ensureMap();
    window.setTimeout( () => {
      this.map.invalidateSize();
      if ( !this.drawn ) this.drawTrip();
      if ( Gps.last && Date.now() - Gps.last.time < 15000 ) this.onFix( Gps.last );
    } , 0 );
    Gps.start( "navigate" );
    this.keepAwake();
    if ( !Gps.last ) this.showGps( I18n.get( "navigate.waiting" ) , false );
  },

  ensureMap() {
    if ( this.map ) return;
    // rotate: leaflet-rotate, for heading-up; turned by setBearing only, never by hand.
    this.map = L.map( "nav-map" , {
      zoomControl: true , attributionControl: true,
      rotate: true , bearing: 0 , rotateControl: false , touchRotate: false , shiftKeyRotate: false,
    } );
    L.tileLayer( "https://tile.openstreetmap.org/{z}/{x}/{y}.png" , {
      maxZoom: 19,
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: I18n.get( "plan.map_attribution" ),
    } ).addTo( this.map );
    this.routeLayer = L.layerGroup().addTo( this.map );
    this.doneLayer = L.layerGroup().addTo( this.map );
    this.pinLayer = L.layerGroup().addTo( this.map );
    this.me = Gps.dot( this.map );
    this.map.setView( [ 40.3 , -82.8 ] , 7 );
    // Moving the map by hand stops it following until Recenter.
    this.map.on( "dragstart" , () => { this.following = false; this.renderRecenter(); } );
  },

  drawTrip() {
    this.drawn = true;
    const styles = getComputedStyle( document.documentElement );
    const back = styles.getPropertyValue( "--plan-back" ).trim() || "#198754";
    const main = styles.getPropertyValue( "--plan-main" ).trim() || "#dc3545";
    this.routeLayer.clearLayers();
    this.pinLayer.clearLayers();
    // A shared route's public link brings the other routes it was offered
    // with: faint and dashed behind the one being driven, as the planner shows them.
    const fastest = styles.getPropertyValue( "--plan-fastest" ).trim() || "#6c757d";
    ( this.trip.others || [] ).forEach( ( other ) => other.runs.forEach( ( run ) => {
      this.routeLayer.addLayer( L.polyline( run.coords , { color: fastest , weight: 4 , opacity: 0.6 , dashArray: "6 8" , interactive: false } ) );
    } ) );
    this.trip.route.runs.forEach( ( run ) => this.routeLayer.addLayer( L.polyline( run.coords , { color: "#ffffff" , weight: 11 , opacity: 0.9 , interactive: false } ) ) );
    this.trip.route.runs.forEach( ( run ) => this.routeLayer.addLayer( L.polyline( run.coords , { color: run.main ? main : back , weight: 7 , opacity: 0.95 , interactive: false } ) ) );
    const pins = this.steps.filter( ( step ) => step.type === "waypoint" || step.type === "arrive" );
    pins.forEach( ( step , index ) => {
      const end = step.type === "arrive";
      this.pinLayer.addLayer( L.marker( [ step.lat , step.lon ] , {
        keyboard: false,
        interactive: false,
        icon: L.divIcon( {
          className: "plan-pin " + ( end ? "plan-pin-end" : "plan-pin-via" ),
          html: "<span>" + ( end ? "B" : String( index + 1 ) ) + "</span>",
          iconSize: [ 28 , 28 ],
          iconAnchor: [ 14 , 14 ],
        } ),
      } ) );
    } );
    if ( this.line.length > 1 ) this.map.fitBounds( L.latLngBounds( this.line ) , { padding: [ 30 , 30 ] , maxZoom: 16 } );
    this.renderSteps();
  },

  // The part already driven, faded, so the line ahead stands out.
  drawDone() {
    this.doneLayer.clearLayers();
    if ( this.along <= 0 ) return;
    const upto = Geo.indexAt( this.cum , this.along );
    const done = this.line.slice( 0 , Math.max( 1 , upto ) );
    if ( this.at ) done.push( this.at );
    if ( done.length > 1 ) this.doneLayer.addLayer( L.polyline( done , { color: "#adb5bd" , weight: 8 , opacity: 1 , interactive: false } ) );
  },

  render() {
    Dom.show( Dom.get( "nav-live" ) , this.active );
    if ( !this.active ) return;
    Dom.text( Dom.get( "nav-title" ) , this.trip.title || I18n.get( "navigate.heading" ) );
    this.renderNext();
    this.renderRecenter();
  },

  renderNext() {
    const step = this.steps[ Math.min( this.next , this.steps.length - 1 ) ];
    const toTurn = Math.max( 0 , step.along - this.along );
    Dom.get( "nav-next-icon" ).className = "bi " + this.icon( step ) + " nav-turn-icon";
    Dom.text( Dom.get( "nav-next-distance" ) , this.arrived ? "" : Format.distance( toTurn ) );
    Dom.text( Dom.get( "nav-next-text" ) , this.arrived ? I18n.get( "navigate.arrived" ) : this.instruction( step ) );
    const then = this.steps[ this.next + 1 ];
    const thenText = then && !this.arrived && step.meters < 400 ? Format.fill( "navigate.then" , { what: this.instruction( then ) } ) : "";
    Dom.text( Dom.get( "nav-then" ) , thenText );
    Dom.show( Dom.get( "nav-then" ) , thenText !== "" );
    const total = this.cum[ this.cum.length - 1 ] || 0;
    const left = this.secondsLeft( this.along );
    const eta = new Date( Date.now() + left * 1000 );
    Dom.text( Dom.get( "nav-remaining" ) , Format.distance( Math.max( 0 , total - this.along ) ) + " · " + Format.duration( left ) );
    Dom.text( Dom.get( "nav-eta" ) , Format.fill( "navigate.eta" , { time: eta.toLocaleTimeString( [] , { hour: "numeric" , minute: "2-digit" } ) } ) );
    Dom.all( "#nav-steps li" ).forEach( ( item , index ) => {
      item.classList.toggle( "active" , index === this.next );
      item.classList.toggle( "text-body-tertiary" , index < this.next );
    } );
  },

  renderSteps() {
    const items = this.steps.map( ( step ) => Dom.el( "li" , {
      class: "list-group-item d-flex align-items-center gap-3",
      children: [
        Dom.el( "i" , { class: "bi " + this.icon( step ) + " fs-4" , attrs: { "aria-hidden": "true" } } ),
        Dom.el( "span" , { class: "me-auto" , text: this.instruction( step ) } ),
        step.meters > 0 ? Dom.el( "span" , { class: "small text-nowrap" , text: Format.distance( step.meters ) } ) : null,
      ].filter( Boolean ),
    } ) );
    Dom.swap( Dom.get( "nav-steps" ) , ...items );
  },

  renderRecenter() {
    Dom.show( Dom.get( "nav-recenter" ) , this.active && !this.following );
  },

  muted() {
    return window.localStorage.getItem( this.MUTE ) === "1";
  },

  setMuted( on ) {
    window.localStorage.setItem( this.MUTE , on ? "1" : "0" );
    this.renderMute();
  },

  renderMute() {
    const muted = this.muted();
    const button = Dom.get( "nav-mute" );
    button.setAttribute( "aria-pressed" , String( muted ) );
    button.querySelector( "i" ).className = "bi " + ( muted ? "bi-volume-mute-fill" : "bi-volume-up-fill" );
    const label = I18n.get( muted ? "navigate.unmute" : "navigate.mute" );
    button.setAttribute( "aria-label" , label );
    button.title = label;
  },

  orientation() {
    return window.localStorage.getItem( this.ORIENT ) === "heading" ? "heading" : "north";
  },

  setOrientation( mode ) {
    window.localStorage.setItem( this.ORIENT , mode );
    this.renderOrientation();
    if ( !this.map ) return;
    if ( mode === "north" ) this.map.setBearing( 0 );
    if ( Gps.last ) {
      this.follow( Gps.last );
      this.me.update( Gps.last );
    }
  },

  renderOrientation() {
    const heading = this.orientation() === "heading";
    const button = Dom.get( "nav-orient" );
    button.setAttribute( "aria-pressed" , String( heading ) );
    button.querySelector( "i" ).className = "bi " + ( heading ? "bi-cursor-fill" : "bi-compass" );
    const label = I18n.get( heading ? "navigate.heading_up" : "navigate.north_up" );
    button.setAttribute( "aria-label" , label );
    button.title = label;
  },

  // The way the car is going, in degrees from north: the GPS's own heading
  // when moving, else the line's direction where the car is on it, else the last.
  headingOf( fix ) {
    if ( typeof fix.heading === "number" && typeof fix.speed === "number" && fix.speed > this.HEADING_MIN_SPEED ) return fix.heading;
    if ( this.line.length > 1 ) {
      const i = Math.min( Geo.indexAt( this.cum , this.along ) , this.line.length - 2 );
      const a = this.line[ i ];
      const b = this.line[ i + 1 ];
      const lat = ( a[ 0 ] + b[ 0 ] ) / 2 * Math.PI / 180;
      const dx = ( b[ 1 ] - a[ 1 ] ) * Math.cos( lat );
      const dy = b[ 0 ] - a[ 0 ];
      if ( dx !== 0 || dy !== 0 ) return ( Math.atan2( dx , dy ) * 180 / Math.PI + 360 ) % 360;
    }
    return this.bearing;
  },

  // Keep the car in view: north-up centres it; heading-up turns the map so
  // the way ahead is up and puts the car below the middle.
  follow( fix , animate ) {
    if ( !this.following || !this.map ) return;
    const zoom = Math.max( this.map.getZoom() , 16 );
    if ( this.orientation() !== "heading" ) {
      if ( this.map.getBearing() !== 0 ) this.map.setBearing( 0 );
      this.map.setView( [ fix.lat , fix.lon ] , zoom , { animate: Boolean( animate ) } );
      return;
    }
    this.bearing = this.headingOf( fix );
    // leaflet-rotate turns the map clockwise by its bearing, so the heading
    // is up when the map is turned back by it.
    this.map.setBearing( ( 360 - this.bearing ) % 360 );
    // Metres per pixel at this zoom, then the point that far ahead along the heading.
    const cos = Math.cos( fix.lat * Math.PI / 180 );
    const metresPerPixel = 40075016.686 * cos / Math.pow( 2 , zoom + 8 );
    const ahead = this.map.getSize().y * this.AHEAD_SHARE * metresPerPixel;
    const rad = this.bearing * Math.PI / 180;
    const lat = fix.lat + ahead * Math.cos( rad ) / 111320;
    const lon = fix.lon + ahead * Math.sin( rad ) / ( 111320 * cos );
    this.map.setView( [ lat , lon ] , zoom , { animate: Boolean( animate ) } );
  },

  showGps( message , bad ) {
    const box = Dom.get( "nav-gps-status" );
    box.className = "alert py-2 mb-2 small " + ( bad ? "alert-danger" : "alert-info" );
    Dom.text( box , message );
    Dom.show( box , Boolean( message ) );
  },

  recenter() {
    this.following = true;
    this.renderRecenter();
    if ( Gps.last ) this.follow( Gps.last );
  },

  // --- words and icons ---------------------------------------------------------------

  icon( step ) {
    if ( step.type === "depart" ) return "bi-geo-alt-fill";
    if ( step.type === "waypoint" ) return "bi-geo-alt-fill";
    if ( step.type === "arrive" ) return "bi-flag-fill";
    return {
      straight: "bi-arrow-up",
      slight_left: "bi-arrow-up-left",
      left: "bi-arrow-left",
      sharp_left: "bi-arrow-down-left",
      slight_right: "bi-arrow-up-right",
      right: "bi-arrow-right",
      sharp_right: "bi-arrow-down-right",
      uturn: "bi-arrow-return-left",
    }[ step.modifier ] || "bi-arrow-up";
  },

  instruction( step ) {
    const onto = step.road ? Format.fill( "navigate.onto" , { road: step.road } ) : "";
    if ( step.type === "depart" ) return step.road ? Format.fill( "navigate.depart_on" , { road: step.road } ) : I18n.get( "navigate.depart" );
    if ( step.type === "arrive" ) return I18n.get( "navigate.arrive" );
    if ( step.type === "waypoint" ) {
      const n = this.steps.filter( ( each ) => each.type === "waypoint" ).indexOf( step ) + 1;
      return Format.fill( "navigate.waypoint" , { n: n } );
    }
    if ( step.type === "continue" ) return step.road ? Format.fill( "navigate.continue_onto" , { road: step.road } ) : I18n.get( "navigate.continue" );
    return Format.fill( "navigate.turn_" + ( step.modifier || "straight" ) , { onto: onto } ).trim();
  },

  say( text ) {
    if ( this.muted() || !window.speechSynthesis || !text ) return;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak( new SpeechSynthesisUtterance( text ) );
  },

  // --- following the GPS ---------------------------------------------------------------

  onFix( fix ) {
    if ( !this.active || !this.line.length ) return;
    this.showGps( fix.accuracy > 100 ? Format.fill( "navigate.weak" , { m: Math.round( fix.accuracy ) } ) : "" , false );
    if ( !this.map ) return;
    this.follow( fix , true );
    this.me.update( fix );
    if ( this.arrived ) return;

    // Ahead first (a little back, for a fix that wobbles), then the whole rest
    // of the line: after a tunnel or a gap the car may be well on.
    const lo = Geo.indexAt( this.cum , this.along - 50 );
    const hi = Geo.indexAt( this.cum , this.along + 1500 ) + 1;
    let on = Geo.snap( this.line , this.cum , fix.lat , fix.lon , Math.max( 0 , lo - 1 ) , hi );
    const limit = this.OFF_M + Math.min( fix.accuracy || 0 , this.OFF_SLACK_M );
    if ( !on || on.off > limit ) {
      const rest = Geo.snap( this.line , this.cum , fix.lat , fix.lon , Math.max( 0 , lo - 1 ) , this.line.length );
      if ( rest && ( !on || rest.off < on.off ) ) on = rest;
    }
    if ( !on ) return;
    if ( on.off > limit ) {
      this.offCount++;
      if ( this.offCount >= this.OFF_FIXES ) this.offRoute( fix );
      return;
    }
    this.offCount = 0;
    Dom.show( Dom.get( "nav-off-route" ) , false );
    // Only ever forward, bar a few metres of wobble.
    if ( on.along >= this.along - 30 ) {
      this.along = Math.max( this.along , on.along );
      this.at = [ on.lat , on.lon ];
    }
    while ( this.next < this.steps.length - 1 && this.steps[ this.next ].along <= this.along + 5 ) this.next++;
    const total = this.cum[ this.cum.length - 1 ] || 0;
    if ( total - this.along < this.ARRIVE_M ) {
      this.arrived = true;
      this.next = this.steps.length - 1;
      this.say( I18n.get( "navigate.arrived" ) );
      Gps.stop( "navigate" );
      this.release();
    } else {
      this.prompt();
    }
    this.drawDone();
    this.renderNext();
  },

  // Speak the next step once at each distance (if the step is longer than
  // that: just after a turn, "in half a mile" would be wrong), and at it.
  prompt() {
    const step = this.steps[ this.next ];
    if ( !step ) return;
    const toTurn = step.along - this.along;
    const previous = this.steps[ this.next - 1 ];
    const length = previous ? step.along - previous.along : toTurn;
    const said = this.spoken[ this.next ] || ( this.spoken[ this.next ] = {} );
    const what = this.instruction( step );
    if ( toTurn <= this.SAY_NOW_M ) {
      if ( !said.now ) { said.now = true; this.say( what ); }
      return;
    }
    for ( const at of this.SAY_AT ) {
      if ( toTurn <= at && length > at + 100 && !said[ at ] ) {
        this.SAY_AT.forEach( ( each ) => { if ( each >= at ) said[ each ] = true; } );
        this.say( Format.fill( "navigate.in" , { distance: Format.distance( toTurn ) , what: what } ) );
        return;
      }
    }
  },

  offRoute( fix ) {
    if ( this.host ) {
      Dom.text( Dom.get( "nav-off-text" ) , I18n.get( "viewer.off_route" ) );
      Dom.show( Dom.get( "nav-off-route" ) , true );
      return;
    }
    const auto = Dom.get( "nav-auto" ).checked;
    Dom.text( Dom.get( "nav-off-text" ) , I18n.get( auto ? "navigate.off_route_auto" : "navigate.off_route" ) );
    Dom.show( Dom.get( "nav-off-route" ) , true );
    if ( auto ) this.reroute( false , fix );
  },

  // Plan the rest of the trip again from where the car is: to the stops
  // still ahead, with the trip's own options. now: the button, which does
  // not wait out REROUTE_MS.
  async reroute( now , fix ) {
    fix = fix || Gps.last;
    if ( !fix || this.rerouting || !this.trip || this.host ) return;
    if ( !now && Date.now() - this.lastReroute < this.REROUTE_MS ) return;
    this.lastReroute = Date.now();
    this.rerouting = true;
    const end = this.line[ this.line.length - 1 ];
    // The stops ahead, on the line past where the car got to, and always the end.
    let from = 0;
    const ahead = [];
    ( this.trip.stops || [] ).forEach( ( stop ) => {
      const on = Geo.snap( this.line , this.cum , stop.lat , stop.lon , from , this.line.length );
      if ( !on ) return;
      from = on.index;
      if ( on.along > this.along + this.ARRIVE_M ) ahead.push( { lat: stop.lat , lon: stop.lon } );
    } );
    const last = ahead[ ahead.length - 1 ];
    if ( !last || Geo.metres( last.lat , last.lon , end[ 0 ] , end[ 1 ] ) > 30 ) ahead.push( { lat: end[ 0 ] , lon: end[ 1 ] } );
    const waypoints = [ { lat: fix.lat , lon: fix.lon } ].concat( ahead.slice( -( this.WAYPOINTS_MAX - 1 ) ) );
    const options = this.trip.options || {};
    const body = {
      waypoints: waypoints,
      penalty: options.penalty || 8,
      road_rules: options.road_rules || {},
      road_factors: options.road_factors,
      sources: options.sources || [ "ours" ],
      compare_fastest: false,
      alternatives: 1,
    };
    if ( !body.road_factors ) delete body.road_factors;
    Dom.text( Dom.get( "nav-off-text" ) , I18n.get( "navigate.rerouting" ) );
    try {
      const result = await Api.post( "/api/route" , body );
      const route = ( result.routes || [] ).find( ( each ) => !each.compare );
      if ( !route ) throw new Error( I18n.get( "navigate.reroute_failed" ) );
      const shape = result.shape || [];
      const stops = ( result.snapped || [] ).filter( ( point , index ) => !shape[ index ] ).slice( 1 ).map( ( point ) => ( { lat: point.lat , lon: point.lon } ) );
      this.load( Object.assign( {} , this.trip , {
        route: { runs: route.runs , steps: route.steps || [] , seconds: route.seconds , meters: route.meters },
        stops: stops,
      } ) );
      this.save();
      this.drawTrip();
      this.drawDone();
      this.following = true;
      Dom.show( Dom.get( "nav-off-route" ) , false );
      this.say( I18n.get( "navigate.rerouted" ) );
      this.render();
    } catch ( error ) {
      Dom.text( Dom.get( "nav-off-text" ) , Format.fill( "navigate.reroute_error" , { message: error.message } ) );
    } finally {
      this.rerouting = false;
    }
  },

  // --- the screen stays on while driving ---------------------------------------------------

  async keepAwake() {
    if ( !( "wakeLock" in navigator ) || this.wake || this.arrived ) return;
    try {
      this.wake = await navigator.wakeLock.request( "screen" );
      this.wake.addEventListener( "release" , () => { this.wake = null; } );
    } catch ( error ) {
      this.wake = null;
    }
  },

  release() {
    if ( this.wake ) this.wake.release().catch( () => {} );
    this.wake = null;
  },
};
