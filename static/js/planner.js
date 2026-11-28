// The Plan a route screen: the stops, the options, the result, and the map
// (map.js) that shows them.
//
// Everything the person changes -- a stop, the order, an option -- goes
// through change(), which records it with Undo so Ctrl+Z puts it back, and
// then plans the route again. The state is small (the stops and the options),
// so each step simply keeps a copy of it from before and after.
//
// Routes are planned by the server (POST /api/route). A newer request makes
// an older answer stale, so only the latest one is shown.
//
// Two kinds of trip: from A to B through the stops, or a loop round a center
// (options.loop.on). The loop has its own points -- the center, and the
// person's own start when it starts there -- so switching between the two
// keeps both. points() is whichever list is in use.
//
// The chosen route can be nudged: dragging its line adds a shape point (a
// small dot, not a stop) that every route then passes through, on the leg it
// was grabbed on. Lock point (in the toolbar) puts a padlock on the route
// where it is clicked; the second one freezes the route before the first and
// after the second: from then on only the middle plans again (POST
// /api/route keep), so nudges there leave the rest exactly as it was. A
// padlock can be dragged along the route. Reset takes nudges and locks away.
// Reverse turns the stops, the nudges and the lock round together, so the
// way back retraces the way there.
// Changing the stops themselves (adding, removing, the loop's
// settings, or any stop while locked) clears them, as one undoable step.
const Planner = {
  stops: [ null , null ],
  loopPoints: [ null , null ],
  // Nudges, in driving order: { lat , lon , leg } (leg as the API counts it).
  shape: [],
  // A locked route: { id , coords , seconds , start , end , middle , trip }.
  // Never changed once made, so undo steps can share it.
  lock: null,
  // The first lock point, [lat, lon] on the route, until the second is set.
  lockPending: null,
  // A route woven by switching onto other routes at forks (see switchAt).
  weave: null,
  // What dragging the route does: nudge it, or show its forks to switch at.
  editMode: "nudge",
  MAX_SHAPE: 12,
  options: {
    backroads: true , penalty: 8 , road_rules: {} , road_factors: {} , compare_fastest: true , sources: [ "ours" ] , combine: "best" , alternatives: 5,
    // A point off our map: plan with the other engines alone (PlanRoute.off_map).
    off_map_engines: true,
    // edits: ring points dropped or moved, by sector (LoopSpec.edits).
    loop: { on: false , radius: 20 , unit: "mi" , start: "center" , direction: "cw" , points: 6 , reuse: 3 , edits: [] },
  },

  // The avoid strengths a road type can be given, and how much a loop may
  // count a road it already drove.
  STRENGTHS: [ 1.5 , 2 , 3 , 5 , 8 , 12 , 25 , 50 , 100 ],
  REUSES: [ 1 , 1.5 , 2 , 3 , 5 , 10 ],
  result: null,
  // Which of the result's routes is shown on the map, saved and exported.
  selected: 0,
  // Recent plans by request, with the route picked in each, so going back
  // to a request tried a moment ago (undo, an option switched back) shows it
  // at once without asking the server.
  cache: new Map(),
  CACHE_SIZE: 20,
  cacheKey: null,
  // Recent places (GET /api/planner/places), newest first: what the stop
  // boxes offer before, and above, a search.
  places: [],
  // A saved route's pick ({source, rank}), chosen once its plan comes back.
  pendingPick: null,
  // The saved route on screen, while what is planned is still that route:
  // { token , id , mine , owner_name , share }. The address bar is its link,
  // /r/<token>; any change to the stops or options lets go of it.
  link: null,
  // After Reverse: { line , waypoints }, the old route turned round, to pick
  // the alternative that retraces it once the reversed trip plans.
  pendingReverse: null,
  ALTERNATIVES: [ 1 , 2 , 3 , 4 , 5 , 6 , 7 , 8 , 9 , 10 , 12 , 15 , 18 , 20 , 24 , 30 , 36 ],
  // Above this many routes, a plan takes noticeably longer (our router plans each one).
  ALTERNATIVES_SLOW: 12,
  picking: null,
  turn: 0,
  status: null,
  planTimer: null,

  async init( me ) {
    this.me = me;
    RouteMap.init( "plan-map" , {
      click: ( lat , lon ) => this.onMapClick( lat , lon ),
      drag: ( index , lat , lon ) => this.setStop( index , this.pointStop( lat , lon ) ),
      nudge: ( fromLat , fromLon , lat , lon ) => this.nudge( fromLat , fromLon , lat , lon ),
      linePick: ( lat , lon ) => this.linePick( lat , lon ),
      moveShape: ( index , lat , lon ) => this.change( "undo.plan_nudge" , ( state ) => {
        state.shape[ index ] = Object.assign( {} , state.shape[ index ] , { lat: lat , lon: lon } );
      } ),
      removeShape: ( index ) => this.change( "undo.plan_nudge" , ( state ) => { state.shape.splice( index , 1 ); } ),
      moveLock: ( which , lat , lon ) => this.moveLock( which , lat , lon ),
      removeRing: ( sector ) => this.editRing( { sector: sector , drop: true } ),
      moveRing: ( sector , lat , lon ) => this.editRing( { sector: sector , lat: lat , lon: lon } ),
    } );
    Dom.get( "plan-undo" ).addEventListener( "click" , () => Undo.undo() );
    Dom.get( "plan-redo" ).addEventListener( "click" , () => Undo.redo() );
    document.addEventListener( "undo-change" , () => this.renderHistory() );
    document.addEventListener( "keydown" , ( event ) => {
      if ( event.key === "Escape" && this.isLockPicking() ) this.startLock();
    } );
    window.addEventListener( "nav" , ( event ) => {
      if ( event.detail.screen !== "plan" ) return;
      window.setTimeout( () => RouteMap.resize() , 0 );
      this.openLinkInPath();
    } );
    window.addEventListener( "resize" , () => RouteMap.resize() );
    this.initLocate();
    this.initSettingsPanel();

    Dom.get( "plan-add-stop" ).addEventListener( "click" , () => this.addStop() );
    Dom.get( "plan-reverse" ).addEventListener( "click" , () => this.reverse() );
    Dom.get( "plan-clear" ).addEventListener( "click" , () => this.clear() );
    Dom.get( "plan-lock" ).addEventListener( "click" , () => this.startLock() );
    Dom.get( "plan-navigate" ).addEventListener( "click" , () => {
      const trip = this.navigation();
      if ( trip ) Navigate.start( trip );
    } );
    Dom.get( "plan-reset-shape" ).addEventListener( "click" , () => this.resetShape() );
    Dom.all( "input[name=plan-edit-mode]" ).forEach( ( radio ) => {
      radio.addEventListener( "change" , () => { if ( radio.checked ) this.setEditMode( radio.value ); } );
    } );
    let mode = "nudge";
    try { mode = window.localStorage.getItem( "plan-edit-mode" ) || "nudge"; } catch ( _error ) { /* private mode */ }
    this.setEditMode( mode );
    Dom.get( "plan-backroads" ).addEventListener( "change" , ( event ) => this.setOption( "backroads" , event.target.checked ) );
    Dom.get( "plan-loop" ).addEventListener( "change" , ( event ) => this.setLoop( "on" , event.target.checked , null , true ) );
    Dom.get( "plan-loop-radius" ).addEventListener( "change" , ( event ) => {
      const value = Number( event.target.value );
      if ( value >= 0.5 && value <= 200 ) this.setLoop( "radius" , value );
      else event.target.value = String( this.options.loop.radius );
    } );
    Dom.get( "plan-loop-unit" ).addEventListener( "change" , ( event ) => this.setLoop( "unit" , event.target.value ) );
    Dom.get( "plan-loop-points" ).addEventListener( "change" , ( event ) => this.setLoop( "points" , Number( event.target.value ) ) );
    Dom.get( "plan-loop-reuse" ).addEventListener( "change" , ( event ) => this.setLoop( "reuse" , Number( event.target.value ) ) );
    Dom.all( "input[name=plan-loop-start]" ).forEach( ( radio ) => {
      radio.addEventListener( "change" , () => { if ( radio.checked ) this.setLoop( "start" , radio.value ); } );
    } );
    Dom.all( "input[name=plan-loop-direction]" ).forEach( ( radio ) => {
      radio.addEventListener( "change" , () => { if ( radio.checked ) this.setLoop( "direction" , radio.value ); } );
    } );
    Dom.get( "plan-compare" ).addEventListener( "change" , ( event ) => this.setOption( "compare_fastest" , event.target.checked ) );
    Dom.get( "plan-off-map" ).addEventListener( "change" , ( event ) => this.setOption( "off_map_engines" , event.target.checked ) );
    Dom.get( "plan-penalty" ).addEventListener( "input" , ( event ) => this.setOption( "penalty" , Number( event.target.value ) , "penalty" ) );
    Dom.all( "#plan-presets [data-penalty]" ).forEach( ( button ) => {
      button.addEventListener( "click" , () => this.setOption( "penalty" , Number( button.getAttribute( "data-penalty" ) ) ) );
    } );
    const many = Dom.get( "plan-alternatives" );
    this.ALTERNATIVES.forEach( ( n ) => many.appendChild( Dom.el( "option" , { text: String( n ) , attrs: { value: String( n ) } } ) ) );
    many.addEventListener( "change" , () => {
      this.setOption( "alternatives" , Number( many.value ) );
      this.renderAlternativesNote();
    } );
    Dom.onPress( Dom.get( "plan-save-prefs" ) , () => this.savePrefs() );
    Dom.onPress( Dom.get( "plan-save" ) , () => this.saveRoute() );
    Dom.onPress( Dom.get( "plan-share" ) , async () => {
      if ( !this.link ) return;
      const updated = await SavedRoutes.share( { id: this.link.id , token: this.link.token , share: this.link.share } );
      if ( updated && this.link && this.link.token === updated.token ) this.link.share = updated.share;
    } );
    Dom.all( "#plan-export-menu [data-export]" ).forEach( ( item ) => {
      item.addEventListener( "click" , () => this.exportAs( item.dataset.export ) );
    } );

    const [ status , prefs , places ] = await Promise.all( [
      Api.request( "/api/routing/status" ),
      Api.request( "/api/planner/prefs" ),
      Api.request( "/api/planner/places" ).catch( () => ( { places: [] } ) ),
    ] );
    this.status = status;
    this.places = places.places;
    this.options = this.copyOptions( this.fromPrefs( prefs.prefs ) );
    this.showStatus();
    RouteMap.showArea( status.bbox );
    this.renderStops();
    this.renderOptions();
  },

  // The owner added or removed a key on the Settings screen: show the sources
  // as they are now.
  async refreshSources() {
    if ( !this.status ) return;
    try {
      this.status = await Api.request( "/api/routing/status" );
    } catch ( error ) {
      return;
    }
    this.renderOptions();
  },

  // --- the map's state ------------------------------------------------------

  showStatus() {
    const box = Dom.get( "plan-map-status" );
    const state = this.status ? this.status.state : "missing";
    let text = "";
    if ( state === "missing" ) text = I18n.get( this.me && this.me.is_owner ? "plan.map_missing_owner" : "plan.map_missing" );
    else if ( state === "loading" ) text = I18n.get( "plan.map_loading" );
    else if ( state === "error" ) text = Format.fill( "plan.map_error" , { error: this.status.error } );
    Dom.text( box , text );
    if ( state === "missing" && this.me && this.me.is_owner ) {
      box.appendChild( document.createTextNode( " " ) );
      box.appendChild( Dom.el( "a" , { class: "alert-link" , text: I18n.get( "plan.map_missing_link" ) , attrs: { href: "/admin/map-data" } } ) );
    }
    Dom.show( box , text !== "" );
    // Still loading: ask again shortly, then plan whatever is waiting.
    if ( state === "loading" ) {
      window.setTimeout( async () => {
        this.status = await Api.request( "/api/routing/status" );
        this.showStatus();
        RouteMap.showArea( this.status.bbox );
        if ( this.status.state === "ready" ) this.plan();
      } , 2000 );
    }
  },

  // --- changes, each one undoable --------------------------------------------

  // Prefs as the API keeps them (loop settings flat) into the options.
  fromPrefs( prefs ) {
    const loop = Object.assign( {} , this.options.loop , {
      radius: prefs.loop_radius , unit: prefs.loop_unit , points: prefs.loop_points , reuse: prefs.loop_reuse,
    } );
    const options = Object.assign( {} , this.options , prefs , { loop: loop } );
    [ "loop_radius" , "loop_unit" , "loop_points" , "loop_reuse" ].forEach( ( name ) => { delete options[ name ]; } );
    return options;
  },

  // Options hold three objects of their own (the road rules, the loop), so a
  // copy copies them too, and no undo step shares them with another.
  copyOptions( options ) {
    return Object.assign( {} , options , {
      road_rules: Object.assign( {} , options.road_rules || {} ),
      road_factors: Object.assign( {} , options.road_factors || {} ),
      loop: Object.assign( {} , options.loop || {} , { edits: ( ( options.loop || {} ).edits || [] ).map( ( edit ) => Object.assign( {} , edit ) ) } ),
      sources: ( options.sources || [] ).slice(),
    } );
  },

  copyPoints( list ) {
    return list.map( ( stop ) => ( stop ? Object.assign( {} , stop ) : null ) );
  },

  snapshot() {
    return {
      stops: this.copyPoints( this.stops ) , loopPoints: this.copyPoints( this.loopPoints ) , options: this.copyOptions( this.options ),
      shape: this.copyPoints( this.shape ) , lock: this.lock , lockPending: this.lockPending , link: this.link,
      weave: this.weave,
    };
  },

  apply( state , fit ) {
    this.stops = this.copyPoints( state.stops );
    this.loopPoints = this.copyPoints( state.loopPoints );
    this.options = this.copyOptions( state.options );
    this.shape = this.copyPoints( state.shape || [] );
    this.lock = state.lock || null;
    this.lockPending = state.lockPending || null;
    this.weave = state.weave || null;
    this.link = state.link || null;
    this.picking = null;
    this.renderLink();
    this.renderStops();
    this.renderOptions();
    this.schedulePlan( fit );
  },

  change( labelKey , mutate , merge , fit ) {
    const before = this.snapshot();
    const after = this.snapshot();
    // What is planned is no longer the saved route (open() sets it again).
    after.link = null;
    mutate( after );
    Undo.run( {
      label: I18n.get( labelKey ),
      merge: merge,
      do: () => this.apply( after , fit ),
      undo: () => this.apply( before , fit ),
    } );
  },

  pointStop( lat , lon ) {
    return { lat: lat , lon: lon , label: Format.fill( "plan.map_point" , { lat: lat.toFixed( 4 ) , lon: lon.toFixed( 4 ) } ) };
  },

  // --- loop or A to B ----------------------------------------------------------

  isLoop() {
    return Boolean( this.options.loop && this.options.loop.on );
  },

  // The loop's own start is used only when it starts there.
  loopUsesStart() {
    return this.options.loop.start === "stop";
  },

  // The points of the trip in use: the stops, or the loop's center (and own start).
  points() {
    if ( !this.isLoop() ) return this.stops;
    return this.loopUsesStart() ? this.loopPoints : this.loopPoints.slice( 0 , 1 );
  },

  // Nudges and the lock belong to the stops as they are: a change to the
  // stops themselves takes them away (mutate's state).
  // A loop's ring edits go too: they belong to the ring as it was.
  unshape( state ) {
    state.shape = [];
    if ( state.options.loop ) state.options.loop.edits = [];
    state.lock = null;
    state.lockPending = null;
    state.weave = null;
  },

  setStop( index , stop , fit ) {
    const loop = this.isLoop();
    const locked = this.lock !== null || this.weave !== null;
    this.change( "undo.plan_stops" , ( state ) => {
      ( loop ? state.loopPoints : state.stops )[ index ] = stop;
      if ( locked ) this.unshape( state );
      // A new center makes a new ring.
      if ( loop && index === 0 ) state.options.loop.edits = [];
    } , null , fit );
  },

  setOption( name , value , merge ) {
    if ( this.options[ name ] === value ) return;
    this.change( "undo.plan_options" , ( state ) => { state.options[ name ] = value; } , merge || null );
  },

  setRule( key , value ) {
    if ( this.options.road_rules[ key ] === value ) return;
    this.change( "undo.plan_options" , ( state ) => { state.options.road_rules[ key ] = value; } );
  },

  // A road type's own avoid strength; null goes back to the slider's.
  setFactor( key , value ) {
    if ( ( this.options.road_factors[ key ] || null ) === value ) return;
    this.change( "undo.plan_options" , ( state ) => {
      if ( value === null ) delete state.options.road_factors[ key ];
      else state.options.road_factors[ key ] = value;
    } );
  },

  // A ring point dropped or moved (RouteMap.drawRing). The nudges go: their
  // legs were counted between the ring points as they were.
  editRing( edit ) {
    if ( !this.isLoop() ) return;
    this.change( "undo.plan_ring" , ( state ) => {
      const edits = state.options.loop.edits.filter( ( old ) => old.sector !== edit.sector );
      edits.push( edit );
      this.unshape( state );
      state.options.loop.edits = edits.sort( ( a , b ) => a.sector - b.sector );
    } );
  },

  setLoop( name , value , merge , fit ) {
    if ( this.options.loop[ name ] === value ) return;
    this.picking = null;
    this.change( "undo.plan_options" , ( state ) => { state.options.loop[ name ] = value; this.unshape( state ); } , merge || null , fit );
  },

  onMapClick( lat , lon ) {
    // Picking a lock point: a click anywhere takes the nearest point of the route.
    if ( this.isLockPicking() ) {
      this.linePick( lat , lon );
      return;
    }
    const points = this.points();
    let index = this.picking;
    if ( index === null || index >= points.length ) {
      index = points.findIndex( ( stop ) => stop === null );
      if ( index < 0 ) index = this.isLoop() ? 0 : points.length - 1;
    }
    this.picking = null;
    this.setStop( index , this.pointStop( lat , lon ) );
  },

  // Close the toolbar's dropdowns so the map is clear to click.
  closeMenus() {
    document.querySelectorAll( "#plan-panel [data-bs-toggle='dropdown']" ).forEach( ( toggle ) => {
      const menu = bootstrap.Dropdown.getInstance( toggle );
      if ( menu ) menu.hide();
    } );
    const panel = bootstrap.Offcanvas.getInstance( Dom.get( "plan-settings" ) );
    if ( panel ) panel.hide();
  },

  addStop() {
    if ( this.isLoop() ) return;
    this.change( "undo.plan_stops" , ( state ) => { state.stops.splice( state.stops.length - 1 , 0 , null ); this.unshape( state ); } );
    this.picking = this.stops.length - 2;
    this.showHint();
  },

  removeStop( index ) {
    this.change( "undo.plan_stops" , ( state ) => { state.stops.splice( index , 1 ); this.unshape( state ); } );
  },

  // The exact opposite: the stops, the nudges and the lock all turned round,
  // and once it plans, the route that best retraces the one picked now.
  reverse() {
    if ( this.isLoop() ) return;
    const line = this.routeLine().slice().reverse();
    let waypoints = null;
    this.change( "undo.plan_stops" , ( state ) => {
      state.stops.reverse();
      state.weave = null;
      const lock = state.lock;
      const legs = lock ? lock.middle.length - 1 : state.stops.filter( Boolean ).length - 1;
      state.shape = state.shape.slice().reverse().map( ( point ) => Object.assign( {} , point , { leg: Math.max( 0 , legs - 1 - point.leg ) } ) );
      if ( lock ) {
        const last = lock.coords.length - 1;
        state.lock = Object.assign( {} , lock , {
          id: this.newLockId(),
          coords: lock.coords.slice().reverse(),
          start: last - lock.end,
          end: last - lock.start,
          middle: lock.middle.slice().reverse(),
          trip: lock.trip.slice().reverse(),
        } );
      }
      waypoints = state.lock ? state.lock.middle : state.stops.filter( Boolean );
    } );
    if ( line.length >= 2 && waypoints ) {
      this.pendingReverse = { line: line , waypoints: this.waypointKey( waypoints ) };
    }
  },

  waypointKey( points ) {
    return JSON.stringify( points.map( ( point ) => [ point.lat , point.lon ] ) );
  },

  // Of the routes to choose between, the one whose line runs closest to line.
  closestTo( choices , line ) {
    const sample = ( points , count ) => {
      const step = Math.max( 1 , Math.floor( points.length / count ) );
      return points.filter( ( point , index ) => index % step === 0 );
    };
    const target = sample( line , 30 );
    let best = 0;
    let bestScore = Infinity;
    choices.forEach( ( route , index ) => {
      const points = [];
      route.runs.forEach( ( run ) => points.push( ...run.coords ) );
      const mine = sample( points , 300 );
      if ( !mine.length ) return;
      let total = 0;
      target.forEach( ( point ) => {
        let near = Infinity;
        mine.forEach( ( other ) => { near = Math.min( near , this.metres( point[ 0 ] , point[ 1 ] , other[ 0 ] , other[ 1 ] ) ); } );
        total += near;
      } );
      if ( total < bestScore ) { bestScore = total; best = index; }
    } );
    return best;
  },

  clear() {
    if ( this.points().every( ( stop ) => stop === null ) ) return;
    const loop = this.isLoop();
    this.change( "undo.plan_stops" , ( state ) => {
      if ( loop ) state.loopPoints = [ null , null ];
      else state.stops = [ null , null ];
      this.unshape( state );
    } );
  },

  // Opening a saved route (saved_routes.js), or one shared with us (shared:
  // its owner_name and mine, from GET /api/shared-routes): its stops and
  // options, undoable like any other change, and the map moves to show it.
  open( saved , shared , navigate ) {
    this.change( "undo.plan_stops" , ( state ) => {
      const points = saved.waypoints.map( ( point ) => ( { lat: point.lat , lon: point.lon , label: point.label } ) );
      const loop = saved.options.loop;
      state.options = this.copyOptions( Object.assign( {} , state.options , {
        backroads: saved.options.backroads,
        penalty: saved.options.penalty,
        road_rules: saved.options.road_rules || {},
        road_factors: saved.options.road_factors || this.statusFactors(),
      } ) );
      if ( loop ) {
        // A loop's points were saved center first, then its own start.
        const center = points[ 0 ] || this.pointStop( loop.center_lat , loop.center_lon );
        state.loopPoints = [ center , points[ 1 ] || null ];
        state.options.loop = Object.assign( {} , state.options.loop , {
          on: true , radius: loop.radius , unit: loop.unit , start: loop.start , direction: loop.direction,
        } );
        if ( loop.points ) state.options.loop.points = loop.points;
        if ( loop.reuse ) state.options.loop.reuse = loop.reuse;
        state.options.loop.edits = ( loop.edits || [] ).map( ( edit ) => Object.assign( {} , edit ) );
      } else {
        state.stops = points;
        state.options.loop.on = false;
      }
      if ( saved.options.sources ) state.options.sources = saved.options.sources.slice();
      if ( saved.options.combine ) state.options.combine = saved.options.combine;
      if ( saved.options.alternatives ) state.options.alternatives = saved.options.alternatives;
      state.shape = ( saved.options.shape || [] ).map( ( point ) => ( { lat: point.lat , lon: point.lon , leg: point.leg } ) );
      const lock = saved.options.lock;
      state.lock = lock ? Object.assign( { id: this.newLockId() , middle: lock.middle } , lock.keep ) : null;
      const follow = saved.options.follow;
      state.weave = follow && !loop ? { id: this.newLockId() , coords: follow.coords , seconds: follow.seconds , trip: follow.trip , pool: [] } : null;
      state.link = saved.token ? this.linkOf( saved , shared ) : null;
    } , null , true );
    this.pendingPick = saved.options.pick || null;
    // Navigate from Saved routes: drive it as soon as its plan is back.
    this.pendingNavigate = Boolean( navigate );
    this.rememberPlaces( saved.waypoints );
    Nav.go( this.link ? this.linkPath( this.link.token ) : "/plan" );
  },

  // A saved route as POST /api/route takes it, the way open() would plan it,
  // for planning it off screen (Saved routes' export with GPX tracks).
  bodyFor( saved ) {
    const options = saved.options || {};
    const usable = new Set( this.sourceList().filter( ( source ) => source.usable ).map( ( source ) => source.id ) );
    let sources = ( options.sources || [ "ours" ] ).filter( ( id ) => usable.has( id ) );
    if ( !sources.length ) sources = [ "ours" ];
    const backroads = options.backroads !== false;
    const body = {
      waypoints: saved.waypoints.map( ( point ) => ( { lat: point.lat , lon: point.lon } ) ),
      penalty: backroads ? options.penalty : 1,
      road_rules: options.road_rules || {},
      road_factors: options.road_factors || this.statusFactors(),
      compare_fastest: false,
      sources: sources,
      alternatives: options.alternatives || 1,
    };
    const loop = options.loop;
    if ( loop ) {
      body.loop = loop;
      body.waypoints = loop.start === "stop" && saved.waypoints[ 1 ] ? [ { lat: saved.waypoints[ 1 ].lat , lon: saved.waypoints[ 1 ].lon } ] : [];
    }
    if ( options.shape && options.shape.length ) body.shape = options.shape.map( ( point ) => ( { lat: point.lat , lon: point.lon , leg: point.leg } ) );
    if ( options.lock ) {
      body.waypoints = options.lock.middle.map( ( point ) => ( { lat: point.lat , lon: point.lon } ) );
      body.keep = options.lock.keep;
      delete body.loop;
    }
    if ( options.follow ) {
      body.follow = options.follow;
      delete body.loop;
      delete body.shape;
      delete body.keep;
    }
    return body;
  },

  // --- a saved route's link (/r/<token>, saved_routes.py) -----------------------

  linkOf( saved , shared ) {
    const mine = shared ? Boolean( shared.mine ) : true;
    return { token: saved.token , id: saved.id , mine: mine , owner_name: shared ? shared.owner_name : "" , share: saved.share || null };
  },

  linkPath( token ) {
    return "/r/" + encodeURIComponent( token );
  },

  // The token in the address bar, when it is a route's link.
  tokenInPath() {
    const match = /^\/r\/([A-Za-z0-9_-]{8,64})\/?$/.exec( window.location.pathname );
    return match ? match[ 1 ] : null;
  },

  // A link opened (pasted, reloaded, Back): the route behind it, if it is
  // ours or shared with us; otherwise say so and show the empty planner.
  async openLinkInPath() {
    const token = this.tokenInPath();
    if ( !token || ( this.link && this.link.token === token ) || this.opening === token ) return;
    this.opening = token;
    try {
      const shared = await Api.request( "/api/shared-routes/" + encodeURIComponent( token ) );
      if ( this.tokenInPath() === token ) this.open( shared.route , shared );
    } catch ( error ) {
      if ( this.tokenInPath() === token ) {
        window.history.replaceState( null , "" , "/plan" );
        Dom.toast( error.status === 404 ? I18n.get( "routes.link_gone" ) : error.message , "error" );
      }
    } finally {
      this.opening = null;
    }
  },

  // The address bar and the banner follow the link: the route's link while
  // it is on screen unchanged, /plan once it is changed.
  renderLink() {
    const link = this.link;
    const banner = Dom.get( "plan-shared" );
    if ( link && !link.mine ) Dom.text( banner , Format.fill( "routes.shared_banner" , { owner: link.owner_name || "?" } ) );
    Dom.show( banner , Boolean( link && !link.mine ) );
    Dom.show( Dom.get( "plan-share" ) , Boolean( link && link.mine ) );
    const path = Nav.clean( window.location.pathname );
    const shown = this.linkShown;
    this.linkShown = Boolean( link );
    if ( path !== "/plan" && !path.startsWith( "/r/" ) ) return;
    // A link being opened (just loaded, its route not here yet) stays put.
    if ( !link && !shown && path.startsWith( "/r/" ) ) return;
    const want = link ? this.linkPath( link.token ) : "/plan";
    if ( path !== want ) window.history.replaceState( null , "" , want );
  },

  // --- the stops card ----------------------------------------------------------

  stopName( index ) {
    if ( this.isLoop() ) return I18n.get( index === 0 ? "plan.loop_center" : "plan.loop_own_start" );
    if ( index === 0 ) return I18n.get( "plan.start" );
    if ( index === this.stops.length - 1 ) return I18n.get( "plan.destination" );
    return Format.fill( "plan.stop" , { n: index } );
  },

  renderStops() {
    const points = this.points();
    const items = points.map( ( stop , index ) => this.stopRow( stop , index ) );
    Dom.swap( Dom.get( "plan-stops" ) , ...items );
    RouteMap.drawStops( points , this.isLoop() ? ( index ) => this.pinKind( index ) : null );
    this.showHint();
  },

  // A pin's look: its class suffix and the letter on it.
  pinKind( index ) {
    if ( this.isLoop() ) return index === 0 ? { kind: "center" , label: "C" } : { kind: "start" , label: "S" };
    const count = this.stops.length;
    return { kind: index === 0 ? "start" : index === count - 1 ? "end" : "via" , label: RouteMap.pinLabel( index , count ) };
  },

  stopRow( stop , index ) {
    const name = this.stopName( index );
    const pin = this.pinKind( index );
    const kind = this.isLoop() ? "loop" : pin.kind;
    const input = Dom.el( "input" , {
      class: "form-control",
      attrs: {
        type: "search",
        value: stop ? stop.label : "",
        placeholder: I18n.get( "plan.search_placeholder" ),
        "aria-label": name,
        autocomplete: "off",
        spellcheck: "false",
      },
    } );
    const menu = Dom.el( "div" , { class: "list-group plan-search-menu shadow-sm" , attrs: { role: "listbox" } } );
    Dom.show( menu , false );
    // Say it is searching as soon as something is typed, not only once the
    // pause before the search has passed.
    input.addEventListener( "input" , () => this.showSearching( index , input , menu ) );
    input.addEventListener( "focus" , () => { if ( input.value.trim() === "" ) this.showRecent( index , input , menu ); } );
    Dom.typed( input , () => this.search( index , input , menu ) , 400 );
    input.addEventListener( "keydown" , ( event ) => {
      if ( event.key === "Escape" ) Dom.show( menu , false );
      if ( event.key === "Enter" ) {
        event.preventDefault();
        const first = menu.querySelector( "button" );
        if ( first ) first.click();
      }
    } );
    input.addEventListener( "blur" , () => window.setTimeout( () => Dom.show( menu , false ) , 200 ) );

    const buttons = [
      Dom.el( "button" , {
        class: "btn btn-outline-secondary",
        attrs: { type: "button" , title: Format.fill( "plan.pick_on_map" , { stop: name } ) , "aria-label": Format.fill( "plan.pick_on_map" , { stop: name } ) },
        children: [ Dom.el( "i" , { class: "bi bi-crosshair" , attrs: { "aria-hidden": "true" } } ) ],
        on: { click: () => { this.picking = index; this.showHint(); this.closeMenus(); } },
      } ),
    ];
    if ( kind === "via" ) {
      buttons.push( Dom.el( "button" , {
        class: "btn btn-outline-secondary",
        attrs: { type: "button" , title: Format.fill( "plan.remove_stop" , { stop: name } ) , "aria-label": Format.fill( "plan.remove_stop" , { stop: name } ) },
        children: [ Dom.el( "i" , { class: "bi bi-x-lg" , attrs: { "aria-hidden": "true" } } ) ],
        on: { click: () => this.removeStop( index ) },
      } ) );
    }
    const badge = Dom.el( "span" , {
      class: "input-group-text plan-stop-badge plan-pin-" + pin.kind,
      text: pin.label,
      attrs: { title: name },
    } );
    return Dom.el( "li" , {
      class: "position-relative",
      children: [
        Dom.el( "div" , { class: "input-group" , children: [ badge , input , ...buttons ] } ),
        menu,
      ],
    } );
  },

  // --- recent places and search ----------------------------------------------

  // Put places (stops, search picks) at the front of the recent list.
  async rememberPlaces( places ) {
    const named = places.filter( ( place ) => place && place.label ).slice( 0 , 10 )
      .map( ( place ) => ( { name: place.label.slice( 0 , 200 ) , lat: place.lat , lon: place.lon } ) );
    if ( !named.length ) return;
    try {
      this.places = ( await Api.post( "/api/planner/places" , { places: named } ) ).places;
    } catch ( error ) {
      // Only a convenience: a failure here leaves the list as it was.
    }
  },

  async clearPlaces( menu ) {
    Dom.show( menu , false );
    try {
      this.places = ( await Api.post( "/api/planner/places/clear" ) ).places;
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    Dom.toast( I18n.get( "plan.recent_cleared" ) , "ok" );
  },

  // Recent places whose name holds query (all of them for "").
  recentMatching( query ) {
    const want = query.toLowerCase();
    return this.places.filter( ( place ) => want === "" || place.name.toLowerCase().includes( want ) );
  },

  // One choice in a stop box's menu; picking it sets the stop and keeps it
  // as recent.
  placeRow( index , menu , place , recent ) {
    const children = [];
    if ( recent ) children.push( Dom.el( "i" , { class: "bi bi-clock-history text-body-secondary me-2" , attrs: { "aria-hidden": "true" } } ) );
    children.push( Dom.el( "span" , { text: place.name } ) );
    return Dom.el( "button" , {
      class: "list-group-item list-group-item-action small",
      attrs: { type: "button" , role: "option" },
      children: children,
      on: { click: () => {
        Dom.show( menu , false );
        const stop = { lat: place.lat , lon: place.lon , label: place.name };
        this.setStop( index , stop , true );
        this.rememberPlaces( [ stop ] );
      } },
    } );
  },

  recentRows( index , menu , places ) {
    return places.map( ( place ) => this.placeRow( index , menu , place , true ) );
  },

  // An empty box: the recent places, and a way to clear them.
  showRecent( index , input , menu ) {
    if ( !this.places.length ) {
      Dom.show( menu , false );
      return;
    }
    Dom.swap( menu,
      Dom.el( "div" , { class: "list-group-item small fw-semibold text-body-secondary" , text: I18n.get( "plan.recent" ) } ),
      ...this.recentRows( index , menu , this.places ),
      Dom.el( "button" , {
        class: "list-group-item list-group-item-action small text-danger",
        attrs: { type: "button" },
        children: [
          Dom.el( "i" , { class: "bi bi-trash me-2" , attrs: { "aria-hidden": "true" } } ),
          Dom.el( "span" , { text: I18n.get( "plan.recent_clear" ) } ),
        ],
        on: { click: () => this.clearPlaces( menu ) },
      } ),
    );
    Dom.show( menu , true );
  },

  // As it is typed: the recent places that match at once, then the search.
  showSearching( index , input , menu ) {
    const query = input.value.trim();
    if ( query === "" ) {
      this.showRecent( index , input , menu );
      return;
    }
    const recent = this.recentRows( index , menu , this.recentMatching( query ) );
    const searching = query.length >= 2 && !( this.status && !this.status.place_search );
    if ( !searching ) {
      Dom.swap( menu , ...recent );
      Dom.show( menu , recent.length > 0 );
      return;
    }
    Dom.swap( menu , ...recent , Dom.el( "div" , { class: "list-group-item small text-body-secondary d-flex align-items-center gap-2" , children: [
      Dom.el( "span" , { class: "spinner-border spinner-border-sm text-primary" , attrs: { "aria-hidden": "true" } } ),
      Dom.el( "span" , { text: Format.fill( "plan.searching" , { query: query } ) } ),
    ] } ) );
    Dom.show( menu , true );
  },

  async search( index , input , menu ) {
    const query = input.value.trim();
    if ( query.length < 2 ) return;
    const recent = this.recentMatching( query );
    if ( this.status && !this.status.place_search ) {
      Dom.swap( menu , ...this.recentRows( index , menu , recent ) ,
        Dom.el( "div" , { class: "list-group-item small text-body-secondary" , text: I18n.get( "plan.search_off" ) } ) );
      Dom.show( menu , true );
      return;
    }
    let places;
    try {
      places = ( await Api.request( "/api/geocode?q=" + encodeURIComponent( query ) ) ).places;
    } catch ( error ) {
      if ( input.value.trim() !== query ) return;
      if ( recent.length ) Dom.swap( menu , ...this.recentRows( index , menu , recent ) );
      else Dom.show( menu , false );
      App.showError( error.message );
      return;
    }
    if ( input.value.trim() !== query ) return;
    // A search answer that is a recent place already listed is left out.
    const fresh = places.filter( ( place ) => !recent.some( ( mine ) =>
      mine.name === place.name || this.metres( mine.lat , mine.lon , place.lat , place.lon ) < 50 ) );
    const rows = [ ...this.recentRows( index , menu , recent ) , ...fresh.map( ( place ) => this.placeRow( index , menu , place , false ) ) ];
    if ( !rows.length ) rows.push( Dom.el( "div" , { class: "list-group-item small text-body-secondary" , text: I18n.get( "plan.search_none" ) } ) );
    Dom.swap( menu , ...rows );
    Dom.show( menu , true );
  },

  showHint() {
    let text;
    const points = this.loopPoints;
    if ( this.isLockPicking() ) text = I18n.get( this.lockPending ? "plan.hint_lock_to" : "plan.hint_lock_from" );
    else if ( this.lockPending ) text = I18n.get( "plan.hint_lock_one" );
    else if ( this.picking !== null ) text = Format.fill( "plan.hint_pick" , { stop: this.stopName( this.picking ) } );
    else if ( this.lock ) text = I18n.get( "plan.hint_locked" );
    else if ( this.isForks() && this.chosen() && !this.isLoop() ) {
      text = I18n.get( this.weave && !( this.weave.pool || [] ).length ? "plan.hint_forks_saved"
        : this.choices().length > 1 || this.weave ? "plan.hint_forks" : "plan.hint_forks_one" );
    }
    else if ( this.isLoop() ) {
      if ( points[ 0 ] === null ) text = I18n.get( "plan.hint_loop_center" );
      else if ( this.loopUsesStart() && points[ 1 ] === null ) text = I18n.get( "plan.hint_loop_start" );
      else text = I18n.get( "plan.hint_loop_done" );
    } else if ( this.stops[ 0 ] === null ) text = I18n.get( "plan.hint_start" );
    else if ( this.stops.some( ( stop ) => stop === null ) ) text = I18n.get( "plan.hint_end" );
    else text = I18n.get( "plan.hint_done" );
    Dom.text( Dom.get( "plan-hint" ) , text );
    Dom.get( "plan-map" ).classList.toggle( "plan-map-picking" , this.picking !== null && !this.isLockPicking() );
  },

  // --- the options card --------------------------------------------------------

  renderOptions() {
    const options = this.options;
    Dom.get( "plan-backroads" ).checked = options.backroads;
    Dom.get( "plan-strength" ).disabled = !options.backroads;
    this.renderRoads();
    this.renderLoop();
    Dom.get( "plan-compare" ).checked = options.compare_fastest;
    Dom.get( "plan-compare" ).disabled = !options.backroads;
    const slider = Dom.get( "plan-penalty" );
    if ( Number( slider.value ) !== options.penalty ) slider.value = String( options.penalty );
    Dom.text( Dom.get( "plan-penalty-value" ) , Format.fill( "plan.penalty_value" , { n: options.penalty } ) );
    Dom.text( Dom.get( "plan-penalty-meaning" ) , Format.fill( "plan.penalty_meaning" , { n: options.penalty } ) );
    Dom.all( "#plan-presets [data-penalty]" ).forEach( ( button ) => {
      const on = Number( button.getAttribute( "data-penalty" ) ) === options.penalty;
      button.classList.toggle( "active" , on );
      button.setAttribute( "aria-pressed" , on ? "true" : "false" );
    } );
    Dom.show( Dom.get( "plan-legend-fastest" ) , options.backroads && options.compare_fastest );
    this.renderSources();
  },

  // The server's own avoid strengths (routing.avoid_strength).
  statusFactors() {
    const out = {};
    ( ( this.status && this.status.road_types ) || [] ).forEach( ( type ) => {
      if ( type.default_strength ) out[ type.id ] = type.default_strength;
    } );
    return out;
  },

  // Under a road type set to Avoid: its strength, the slider's or its own.
  strengthSelect( type ) {
    const current = this.options.road_factors[ type.id ] || null;
    const values = this.STRENGTHS.slice();
    if ( current !== null && !values.includes( current ) ) values.push( current );
    values.sort( ( a , b ) => a - b );
    const select = Dom.el( "select" , {
      class: "form-select form-select-sm w-auto",
      attrs: { "aria-label": Format.fill( "plan.strength_label" , { road: I18n.get( "plan.road_" + type.id ) } ) },
      children: [
        Dom.el( "option" , { text: Format.fill( "plan.strength_slider" , { n: this.options.penalty } ) , attrs: { value: "" } } ),
        ...values.map( ( n ) => Dom.el( "option" , { text: Format.fill( "plan.strength_value" , { n: n } ) , attrs: { value: String( n ) } } ) ),
      ],
      on: { change: ( event ) => this.setFactor( type.id , event.target.value === "" ? null : Number( event.target.value ) ) },
    } );
    select.value = current === null ? "" : String( current );
    select.disabled = !this.options.backroads;
    return Dom.el( "div" , { class: "d-flex justify-content-end mb-1" , children: [ select ] } );
  },

  // One row per road type: its name and Use / Avoid / Never, and under an
  // avoided one its strength.
  renderRoads() {
    const types = ( this.status && this.status.road_types ) || [];
    const rules = this.options.road_rules;
    const rows = types.map( ( type ) => {
      const value = rules[ type.id ] || type.default;
      const name = "plan-road-" + type.id;
      const label = Dom.el( "span" , { class: "small me-auto" , text: I18n.get( "plan.road_" + type.id ) , attrs: { id: name + "-label" } } );
      const buttons = [ [ "use" , "btn-outline-success" ] , [ "avoid" , "btn-outline-warning" ] , [ "never" , "btn-outline-danger" ] ].flatMap( ( [ rule , style ] ) => {
        const id = name + "-" + rule;
        const input = Dom.el( "input" , {
          class: "btn-check",
          attrs: { type: "radio" , name: name , id: id , value: rule , autocomplete: "off" },
          on: { change: ( event ) => { if ( event.target.checked ) this.setRule( type.id , rule ); } },
        } );
        input.checked = value === rule;
        return [ input , Dom.el( "label" , { class: "btn " + style , text: I18n.get( "plan.rule_" + rule ) , attrs: { for: id } } ) ];
      } );
      const row = Dom.el( "div" , { class: "d-flex align-items-center gap-2" , children: [
        label,
        Dom.el( "div" , { class: "btn-group btn-group-sm flex-shrink-0" , attrs: { role: "group" , "aria-labelledby": name + "-label" } , children: buttons } ),
      ] } );
      return value === "avoid" ? Dom.el( "div" , { children: [ row , this.strengthSelect( type ) ] } ) : row;
    } );
    Dom.swap( Dom.get( "plan-roads" ) , ...rows );
    const restricted = rules.restricted && rules.restricted !== "never";
    Dom.show( Dom.get( "plan-roads-rebuild" ) , Boolean( restricted && this.status && !this.status.restricted_on_map ) );
  },

  renderLoop() {
    const loop = this.options.loop;
    Dom.get( "plan-loop" ).checked = loop.on;
    Dom.show( Dom.get( "plan-loop-settings" ) , loop.on );
    Dom.show( Dom.get( "plan-add-stop" ) , !loop.on );
    Dom.show( Dom.get( "plan-reverse" ) , !loop.on );
    const radius = Dom.get( "plan-loop-radius" );
    if ( Number( radius.value ) !== loop.radius ) radius.value = String( loop.radius );
    Dom.get( "plan-loop-unit" ).value = loop.unit;
    Dom.get( "plan-loop-points" ).value = String( loop.points );
    const reuses = this.REUSES.includes( loop.reuse ) ? this.REUSES : [ ...this.REUSES , loop.reuse ].sort( ( a , b ) => a - b );
    const reuse = Dom.get( "plan-loop-reuse" );
    Dom.swap( reuse , ...reuses.map( ( n ) => Dom.el( "option" , {
      text: n === 1 ? I18n.get( "plan.loop_reuse_free" ) : Format.fill( "plan.loop_reuse_value" , { n: n } ),
      attrs: { value: String( n ) },
    } ) ) );
    reuse.value = String( loop.reuse );
    Dom.get( "plan-loop-start-" + loop.start ).checked = true;
    Dom.get( "plan-loop-" + loop.direction ).checked = true;
  },

  // The road rules an engine cannot be told, among the ones in force now.
  notTold( source ) {
    if ( !source.honours || source.id === "ours" ) return [];
    const rules = this.options.road_rules;
    const backroads = this.options.backroads;
    const off = ( ...keys ) => keys.some( ( key ) => rules[ key ] === "never" || ( backroads && rules[ key ] === "avoid" ) );
    const asked = { freeways: off( "interstate" ) , major: off( "us_highway" , "state_route" ) , unpaved: off( "unpaved" ) , tracks: off( "track" ) };
    return Object.keys( asked ).filter( ( name ) => asked[ name ] && !source.honours.includes( name ) );
  },

  // --- the sources card ----------------------------------------------------------

  sourceList() {
    return ( this.status && this.status.sources ) || [ { id: "ours" , name: I18n.get( "plan.source_ours" ) , usable: true , backroads: "" } ];
  },

  // The picked sources that can be asked now.
  usableSources() {
    const usable = new Set( this.sourceList().filter( ( source ) => source.usable ).map( ( source ) => source.id ) );
    return ( this.options.sources || [] ).filter( ( id ) => usable.has( id ) );
  },

  toggleSource( id , on ) {
    const picked = new Set( this.options.sources || [] );
    if ( on ) picked.add( id ); else picked.delete( id );
    const order = this.sourceList().map( ( source ) => source.id );
    this.setOption( "sources" , order.filter( ( name ) => picked.has( name ) ) );
  },

  renderSources() {
    const picked = new Set( this.options.sources || [] );
    const rows = this.sourceList().map( ( source ) => {
      const id = "plan-source-" + source.id;
      const input = Dom.el( "input" , {
        class: "form-check-input",
        attrs: { type: "checkbox" , id: id },
        on: { change: ( event ) => this.toggleSource( source.id , event.target.checked ) },
      } );
      input.checked = picked.has( source.id ) && source.usable;
      input.disabled = !source.usable;
      const label = Dom.el( "label" , { class: "form-check-label" , attrs: { for: id , title: source.backroads || "" } , children: [
        Dom.el( "span" , { text: source.id === "ours" ? I18n.get( "plan.source_ours" ) : source.name } ),
      ] } );
      if ( !source.usable ) {
        // Anyone can add their own key on Settings (Nav follows the link).
        const note = source.needs_key
          ? Dom.el( "a" , { class: "small ms-2" , text: I18n.get( "plan.source_unusable_owner" ) , attrs: { href: "/settings" , title: source.why_not } } )
          : Dom.el( "span" , { class: "small text-body-secondary ms-2" , text: I18n.get( "plan.source_unusable" ) , attrs: { title: source.why_not } } );
        label.appendChild( note );
      }
      const missing = source.usable && picked.has( source.id ) ? this.notTold( source ) : [];
      const told = missing.length ? Dom.el( "div" , { class: "small text-body-secondary" , text: Format.fill( "plan.source_not_told" , {
        roads: missing.map( ( name ) => I18n.get( "plan.told_" + name ) ).join( ", " ),
      } ) } ) : null;
      return Dom.el( "div" , { class: "form-check" , children: [ input , label , told ].filter( Boolean ) } );
    } );
    Dom.swap( Dom.get( "plan-sources" ) , ...rows );
    Dom.get( "plan-off-map" ).checked = this.options.off_map_engines !== false;
    Dom.get( "plan-alternatives" ).value = String( this.options.alternatives || 1 );
    this.renderAlternativesNote();
  },

  // The toolbar's four menus (stops, options, sources, routes to offer) are
  // dropdowns from lg up; on a phone they are one panel (#plan-settings) of
  // accordion sections. Each menu's body is one node, moved between the two,
  // so every control keeps its id and listeners.
  SETTINGS_SECTIONS: [ "stops" , "how" , "sources" , "alternatives" ],

  initSettingsPanel() {
    const wide = window.matchMedia( "(min-width: 992px)" );
    const place = () => {
      this.SETTINGS_SECTIONS.forEach( ( name ) => {
        const body = Dom.get( "plan-body-" + name );
        const home = wide.matches ? Dom.get( "plan-menu-" + name ) : document.querySelector( "#plan-settings [data-slot=" + name + "]" );
        if ( body && home && body.parentNode !== home ) home.appendChild( body );
      } );
      if ( wide.matches ) {
        const panel = bootstrap.Offcanvas.getInstance( Dom.get( "plan-settings" ) );
        if ( panel ) panel.hide();
      }
    };
    place();
    wide.addEventListener( "change" , place );
  },

  // Under "Routes to offer": how many came back when fewer than asked for,
  // and that a big number is slow (but spends no more of other engines' quota).
  renderAlternativesNote() {
    const asked = this.options.alternatives || 1;
    const found = this.result ? this.choices().length : 0;
    const lines = [];
    if ( this.result && found < asked ) lines.push( Format.fill( "plan.alternatives_found" , { n: found , asked: asked } ) );
    if ( asked > this.ALTERNATIVES_SLOW ) lines.push( I18n.get( "plan.alternatives_slow" ) );
    Dom.text( Dom.get( "plan-alternatives-count" ) , String( asked ) );
    const note = Dom.get( "plan-alternatives-note" );
    note.textContent = lines.join( " " );
    Dom.show( note , lines.length > 0 );
  },

  async savePrefs() {
    const options = this.options;
    await Api.post( "/api/planner/prefs" , {
      backroads: options.backroads , penalty: options.penalty , road_rules: options.road_rules , road_factors: options.road_factors,
      compare_fastest: options.compare_fastest , sources: options.sources , combine: options.combine || "best",
      alternatives: options.alternatives || 1, off_map_engines: options.off_map_engines !== false,
      loop_radius: options.loop.radius , loop_unit: options.loop.unit , loop_points: options.loop.points , loop_reuse: options.loop.reuse,
    } );
    Dom.toast( I18n.get( "plan.prefs_saved" ) , "ok" );
  },

  // --- planning ------------------------------------------------------------------

  ready() {
    if ( this.isLoop() ) return this.points().every( ( stop ) => stop !== null );
    return this.stops.length >= 2 && this.stops[ 0 ] !== null && this.stops[ this.stops.length - 1 ] !== null;
  },

  // The loop as the API takes it (LoopSpec).
  loopSpec() {
    const loop = this.options.loop;
    const center = this.loopPoints[ 0 ];
    return {
      center_lat: center.lat , center_lon: center.lon , radius: loop.radius , unit: loop.unit , start: loop.start , direction: loop.direction,
      points: loop.points , reuse: loop.reuse,
      edits: ( loop.edits || [] ).length ? loop.edits.map( ( edit ) => Object.assign( {} , edit ) ) : null,
    };
  },

  schedulePlan( fit ) {
    window.clearTimeout( this.planTimer );
    this.planTimer = window.setTimeout( () => this.plan( fit ) , 250 );
  },

  async plan( fit ) {
    const turn = ++this.turn;
    if ( !this.ready() || !this.status || this.status.state !== "ready" ) {
      this.showBusy( null );
      this.result = null;
      RouteMap.clearRoutes();
      this.renderShapeTools();
      Dom.show( Dom.get( "plan-result" ) , false );
      if ( fit ) RouteMap.fitTo( [] , this.points() );
      return;
    }
    const options = this.options;
    const sources = this.usableSources();
    if ( sources.length === 0 ) {
      this.showBusy( null );
      this.result = null;
      RouteMap.clearRoutes();
      Dom.show( Dom.get( "plan-result" ) , false );
      App.showError( I18n.get( "plan.sources_need_one" ) );
      return;
    }
    const loop = this.isLoop();
    const body = {
      waypoints: loop
        ? ( this.loopUsesStart() ? [ { lat: this.loopPoints[ 1 ].lat , lon: this.loopPoints[ 1 ].lon } ] : [] )
        : this.stops.filter( Boolean ).map( ( stop ) => ( { lat: stop.lat , lon: stop.lon } ) ),
      penalty: options.backroads ? options.penalty : 1,
      road_rules: options.road_rules,
      road_factors: options.road_factors,
      compare_fastest: options.backroads && options.compare_fastest,
      sources: sources,
      alternatives: options.alternatives || 1,
    };
    if ( options.off_map_engines !== false ) body.off_map = "engines";
    if ( loop ) body.loop = this.loopSpec();
    this.lastBody = body;
    if ( this.shape.length ) body.shape = this.shape.map( ( point ) => ( { lat: point.lat , lon: point.lon , leg: point.leg } ) );
    // Locked: only the middle plans again, through its own points; the key
    // names the lock rather than repeating its whole line.
    let key;
    if ( this.weave ) {
      const weave = this.weave;
      delete body.loop;
      delete body.shape;
      key = JSON.stringify( Object.assign( {} , body , { follow: "weave-" + weave.id } ) );
      body.follow = { coords: weave.coords , seconds: weave.seconds , trip: weave.trip };
    } else if ( this.lock ) {
      const lock = this.lock;
      body.waypoints = lock.middle.map( ( point ) => ( { lat: point.lat , lon: point.lon } ) );
      delete body.loop;
      key = JSON.stringify( Object.assign( {} , body , { keep: "lock-" + lock.id } ) );
      body.keep = { coords: lock.coords , seconds: lock.seconds , start: lock.start , end: lock.end , trip: lock.trip };
    } else {
      key = JSON.stringify( body );
    }
    const cached = this.cache.get( key );
    if ( cached ) {
      // Seen a moment ago: show it as it was, with the route picked then.
      this.cache.delete( key );
      this.cache.set( key , cached );
      this.show( key , cached.result , cached.selected , fit );
      this.showBusy( null );
      return;
    }
    const summary = Dom.get( "plan-summary" );
    summary.classList.add( "opacity-50" );
    this.showBusy( "updating" );
    let result;
    try {
      result = await Api.post( "/api/route" , body );
    } catch ( error ) {
      if ( turn !== this.turn ) return;
      summary.classList.remove( "opacity-50" );
      this.showBusy( null );
      this.pendingNavigate = false;
      // A point off our map, with the fallback switched off: offer it.
      if ( error.payload && error.payload.off_map && options.off_map_engines === false ) {
        Dom.toast( error.message , "error" , { text: I18n.get( "plan.off_map_try_engines" ) , run: () => this.setOption( "off_map_engines" , true ) } );
      } else {
        App.showError( error.message );
      }
      return;
    } finally {
      if ( turn === this.turn ) summary.classList.remove( "opacity-50" );
    }
    if ( turn !== this.turn ) return;
    // Say whether the change moved the route, so a setting that makes no
    // difference here does not look like it was ignored.
    const before = this.result ? this.signature( this.result ) : null;
    this.showBusy( before === null ? null : before === this.signature( result ) ? "unchanged" : "updated" );
    this.cache.set( key , { result: result , selected: 0 } );
    while ( this.cache.size > this.CACHE_SIZE ) this.cache.delete( this.cache.keys().next().value );
    let selected = 0;
    if ( this.pendingPick ) {
      const pick = this.pendingPick;
      const found = result.routes.filter( ( route ) => !route.compare ).findIndex( ( route ) => route.source === pick.source && ( route.rank || 0 ) === pick.rank );
      if ( found >= 0 ) selected = found;
      this.pendingPick = null;
    }
    const turned = this.pendingReverse;
    if ( turned && turned.waypoints === this.waypointKey( body.waypoints ) ) {
      selected = this.closestTo( result.routes.filter( ( route ) => !route.compare ) , turned.line );
      this.cache.get( key ).selected = selected;
    }
    this.show( key , result , selected , fit );
  },

  show( key , result , selected , fit ) {
    this.pendingReverse = null;
    this.cacheKey = key;
    this.result = result;
    this.selected = selected;
    this.remember();
    this.draw();
    if ( fit || !this.shownOnce ) RouteMap.fitTo( result.routes , this.points() );
    this.shownOnce = true;
    this.renderResult( result );
    if ( this.pendingNavigate ) {
      this.pendingNavigate = false;
      const trip = this.navigation();
      if ( trip ) Navigate.start( trip );
    }
  },

  // Keep the pick with the cached plan.
  remember() {
    const entry = this.cacheKey && this.cache.get( this.cacheKey );
    if ( entry ) entry.selected = this.selected;
  },

  signature( result ) {
    return result.routes.map( ( route ) => [ route.kind , route.source , Math.round( route.seconds ) , Math.round( route.meters ) ].join( ":" ) ).join( "|" );
  },

  // The badge over the map: a spinner while a route is being planned, then
  // what came of it for a moment. null hides it.
  showBusy( state ) {
    const badge = Dom.get( "plan-busy" );
    window.clearTimeout( this.busyTimer );
    if ( !state ) {
      Dom.show( badge , false );
      return;
    }
    const icon = state === "updating"
      ? Dom.el( "span" , { class: "spinner-border spinner-border-sm text-primary" , attrs: { "aria-hidden": "true" } } )
      : Dom.el( "i" , { class: state === "updated" ? "bi bi-check-circle-fill text-success" : "bi bi-info-circle-fill text-secondary" , attrs: { "aria-hidden": "true" } } );
    Dom.swap( badge , icon , Dom.el( "span" , { text: I18n.get( "plan." + state ) } ) );
    Dom.show( badge , true );
    if ( state !== "updating" ) this.busyTimer = window.setTimeout( () => Dom.show( badge , false ) , 2500 );
  },

  // The routes to choose between, and the fastest one shown to compare.
  choices() {
    return this.result ? this.result.routes.filter( ( route ) => !route.compare ) : [];
  },

  comparison() {
    return this.result ? this.result.routes.find( ( route ) => route.compare ) || null : null;
  },

  chosen() {
    const choices = this.choices();
    return choices[ Math.min( this.selected , choices.length - 1 ) ] || null;
  },

  // The chosen route on top, the others faint behind it.
  draw() {
    const chosen = this.chosen();
    if ( !chosen ) {
      RouteMap.clearRoutes();
      return;
    }
    const choices = this.choices();
    RouteMap.drawRoutes( [ chosen , ...this.poolRoutes() , ...this.result.routes.filter( ( route ) => route !== chosen ) ] , ( route ) => {
      const index = choices.indexOf( route );
      if ( index >= 0 ) this.select( index );
    } );
    if ( this.isLoop() && !this.lock && this.result.ring && this.result.ring.length ) {
      const loop = this.options.loop;
      RouteMap.drawRing( this.loopPoints[ 0 ] , this.result.ring , loop.unit === "mi" ? loop.radius * 1609.344 : null , this.result.ring_sectors );
    }
    this.drawShaping();
  },

  // The grabbable part of the line, the nudges and the padlocks.
  drawShaping() {
    const line = this.routeLine();
    if ( line.length < 2 ) {
      RouteMap.drawEdit( null );
      RouteMap.drawShape( [] );
      RouteMap.drawLocks( [] );
      RouteMap.drawForks( [] );
      return;
    }
    const picking = this.isLockPicking();
    const [ lo , hi ] = picking ? [ 0 , line.length - 1 ] : this.editRange( line );
    RouteMap.drawEdit( line.slice( lo , hi + 1 ) , !picking && !this.isForks() );
    RouteMap.drawForks( this.forkPins() );
    // A nudge sits where it met the road, once a plan has put it there.
    const met = this.result ? ( this.result.snapped || [] ).filter( ( point , index ) => ( this.result.shape || [] )[ index ] ) : [];
    RouteMap.drawShape( met.length === this.shape.length ? met : this.shape );
    const locks = [];
    if ( this.lock ) locks.push( this.lock.coords[ this.lock.start ] , this.lock.coords[ this.lock.end ] );
    if ( this.lockPending ) locks.push( this.lockPending );
    RouteMap.drawLocks( locks );
  },

  // --- nudging and locking ---------------------------------------------------

  isLockPicking() {
    return this.picking === "lock";
  },

  newLockId() {
    return Date.now().toString( 36 ) + Math.random().toString( 36 ).slice( 2 , 8 );
  },

  // --- my location and Navigate ------------------------------------------------

  // The map's crosshair: shows where the device is and follows it until
  // pressed again. The dot (or Start from my location in Where) makes it
  // the start.
  initLocate() {
    this.meDot = Gps.dot( RouteMap.map , ( fix , marker ) => {
      const button = Dom.el( "button" , {
        class: "btn btn-sm btn-primary",
        attrs: { type: "button" },
        text: I18n.get( "plan.start_here" ),
        on: { click: () => { RouteMap.map.closePopup(); this.useMyLocation( fix ); } },
      } );
      marker.unbindPopup();
      marker.bindPopup( button ).openPopup();
    } );
    let button = null;
    let centred = false;
    RouteMap.addLocate( ( pressed ) => {
      button = pressed;
      const on = button.getAttribute( "aria-pressed" ) !== "true";
      button.setAttribute( "aria-pressed" , String( on ) );
      button.classList.toggle( "active" , on );
      centred = false;
      if ( on ) Gps.start( "plan" );
      else { Gps.stop( "plan" ); this.meDot.clear(); }
    } );
    window.addEventListener( "gps" , ( event ) => {
      if ( !Gps.wanted.has( "plan" ) ) return;
      this.meDot.update( event.detail );
      if ( !centred ) {
        centred = true;
        RouteMap.map.setView( [ event.detail.lat , event.detail.lon ] , Math.max( RouteMap.map.getZoom() , 14 ) );
      }
    } );
    window.addEventListener( "gps-error" , ( event ) => {
      if ( button && !Gps.wanted.has( "plan" ) ) {
        button.setAttribute( "aria-pressed" , "false" );
        button.classList.remove( "active" );
      }
      if ( Nav.current() === "plan" ) App.showError( event.detail.message );
    } );
    const here = Dom.get( "plan-from-here" );
    if ( here ) {
      here.addEventListener( "click" , async () => {
        let fix;
        here.disabled = true;
        try {
          fix = await Gps.once();
        } catch ( error ) {
          return;
        } finally {
          here.disabled = false;
        }
        this.useMyLocation( fix );
      } );
    }
  },

  // The device's position as the trip's start (a loop's own start, or its
  // center when it circles the center), undoable like any other stop.
  useMyLocation( fix ) {
    const stop = { lat: fix.lat , lon: fix.lon , label: I18n.get( "plan.my_location" ) };
    const index = this.isLoop() && this.loopUsesStart() ? 1 : 0;
    this.setStop( index , stop , true );
  },

  // What the Navigate screen drives: the chosen route as shown (its line and
  // steps), the stops it passes, and the options to plan again with when
  // the driver leaves it. null with no route.
  navigation() {
    const route = this.chosen();
    if ( !route || !this.result ) return null;
    const body = this.lastBody || {};
    const shape = this.result.shape || [];
    const stops = ( this.result.snapped || [] ).filter( ( point , index ) => !shape[ index ] ).map( ( point ) => ( { lat: point.lat , lon: point.lon } ) );
    let title = "";
    try { title = this.defaultName(); } catch ( error ) { title = ""; }
    return {
      title: ( this.link && this.link.name ) || title,
      route: { runs: route.runs , steps: route.steps || [] , seconds: route.seconds , meters: route.meters },
      stops: stops,
      options: {
        penalty: body.penalty , road_rules: body.road_rules , road_factors: body.road_factors,
        sources: ( body.sources || [ "ours" ] ).includes( "ours" ) ? [ "ours" ] : body.sources,
      },
    };
  },

  // The chosen route as one line of [lat, lon] pairs.
  routeLine() {
    const route = this.chosen();
    const out = [];
    if ( !route ) return out;
    route.runs.forEach( ( run ) => run.coords.forEach( ( point , index ) => {
      const last = out[ out.length - 1 ];
      if ( index === 0 && last && last[ 0 ] === point[ 0 ] && last[ 1 ] === point[ 1 ] ) return;
      out.push( point );
    } ) );
    return out;
  },

  // Rough metres between two points (plenty to compare nearness).
  metres( lat1 , lon1 , lat2 , lon2 ) {
    const x = ( lon2 - lon1 ) * Math.cos( ( ( lat1 + lat2 ) / 2 ) * Math.PI / 180 );
    return Math.hypot( x , lat2 - lat1 ) * 111195;
  },

  // The line's point nearest (lat, lon) between lo and hi. near: the first
  // one closer than that many metres wins, so a loop that passes the same
  // place twice finds the first pass.
  nearest( line , lat , lon , lo , hi , near ) {
    let best = lo;
    let bestM = Infinity;
    for ( let index = lo ; index <= hi && index < line.length ; index++ ) {
      const m = this.metres( lat , lon , line[ index ][ 0 ] , line[ index ][ 1 ] );
      if ( near && m < near ) return index;
      if ( m < bestM ) { bestM = m; best = index; }
    }
    return best;
  },

  // Where the plan's points (result.snapped: waypoints and nudges) fall on
  // the line, in order: { vertex , shape , lat , lon }.
  marks( line , lo ) {
    const out = [];
    let from = lo;
    ( ( this.result && this.result.snapped ) || [] ).forEach( ( point , index ) => {
      const vertex = this.nearest( line , point.lat , point.lon , from , line.length - 1 , 25 );
      out.push( { vertex: vertex , shape: Boolean( ( this.result.shape || [] )[ index ] ) , lat: point.lat , lon: point.lon } );
      from = vertex;
    } );
    return out;
  },

  // The part of the line that plans again: all of it, or between the locks.
  editRange( line ) {
    if ( !this.lock || line.length < 2 ) return [ 0 , line.length - 1 ];
    const from = this.lock.coords[ this.lock.start ];
    const to = this.lock.coords[ this.lock.end ];
    const lo = this.nearest( line , from[ 0 ] , from[ 1 ] , 0 , line.length - 1 , 1 );
    let hi = line.length - 1;
    for ( let index = line.length - 1 ; index > lo ; index-- ) {
      if ( this.metres( to[ 0 ] , to[ 1 ] , line[ index ][ 0 ] , line[ index ][ 1 ] ) < 1 ) { hi = index; break; }
    }
    return [ lo , hi ];
  },

  // The line was grabbed at (fromLat, fromLon) and dropped at (lat, lon): a
  // new nudge, on the leg it was grabbed on, in order among that leg's.
  nudge( fromLat , fromLon , lat , lon ) {
    const line = this.routeLine();
    if ( line.length < 2 || !this.result ) return;
    if ( this.shape.length >= this.MAX_SHAPE ) {
      App.showError( I18n.get( "plan.nudge_too_many" ) );
      return;
    }
    const [ lo , hi ] = this.editRange( line );
    const grab = this.nearest( line , fromLat , fromLon , lo , hi );
    const marks = this.marks( line , lo );
    const stops = marks.filter( ( mark ) => !mark.shape ).length;
    let seen = 0;
    marks.forEach( ( mark ) => { if ( !mark.shape && mark.vertex <= grab ) seen++; } );
    const leg = Math.max( 0 , Math.min( seen - 1 , stops - 2 ) );
    const at = marks.filter( ( mark ) => mark.shape ).map( ( mark ) => mark.vertex );
    let place = this.shape.findIndex( ( point , index ) => point.leg > leg || ( point.leg === leg && at.length === this.shape.length && at[ index ] > grab ) );
    if ( place < 0 ) place = this.shape.length;
    if ( this.weave ) Dom.toast( I18n.get( "plan.weave_cleared" ) , "ok" );
    this.change( "undo.plan_nudge" , ( state ) => { state.weave = null; state.shape.splice( place , 0 , { lat: lat , lon: lon , leg: leg } ); } );
  },

  // The toolbar's Lock point: wait for a click on the route (again to cancel).
  startLock() {
    if ( !this.chosen() ) return;
    this.picking = this.isLockPicking() ? null : "lock";
    this.closeMenus();
    this.showHint();
    this.drawShaping();
    this.renderShapeTools();
  },

  // A click on (or near) the route while placing a lock point. The first
  // one waits for the second; the second locks. false: not placing one, so
  // the map treats it as a grab.
  linePick( lat , lon ) {
    if ( !this.isLockPicking() ) return false;
    const line = this.routeLine();
    if ( line.length < 2 ) return true;
    const vertex = this.nearest( line , lat , lon , 0 , line.length - 1 );
    if ( !this.lockPending ) {
      this.change( "undo.plan_lock" , ( state ) => { state.lockPending = line[ vertex ].slice(); } );
      // Still placing: the second point is next.
      this.picking = "lock";
      this.showHint();
      this.drawShaping();
      this.renderShapeTools();
      return true;
    }
    const first = this.nearest( line , this.lockPending[ 0 ] , this.lockPending[ 1 ] , 0 , line.length - 1 );
    this.makeLock( line , first , vertex );
    return true;
  },

  // A padlock dragged along the route: 0 and 1 the lock's ends, 2 the first
  // point still waiting for its second.
  moveLock( which , lat , lon ) {
    const line = this.routeLine();
    if ( line.length < 2 ) return;
    const vertex = this.nearest( line , lat , lon , 0 , line.length - 1 );
    if ( which === 2 || !this.lock ) {
      this.change( "undo.plan_lock" , ( state ) => { state.lockPending = line[ vertex ].slice(); } );
      return;
    }
    const other = this.lock.coords[ which === 0 ? this.lock.end : this.lock.start ];
    const otherVertex = this.nearest( line , other[ 0 ] , other[ 1 ] , 0 , line.length - 1 , 1 );
    if ( !this.makeLock( line , otherVertex , vertex ) ) this.draw();
  },

  makeLock( line , first , second ) {
    const start = Math.min( first , second );
    const end = Math.max( first , second );
    if ( end - start < 2 ) {
      App.showError( I18n.get( "plan.lock_too_short" ) );
      return false;
    }
    const marks = this.marks( line , 0 );
    // The points the middle still has to pass: the plan's own (not nudges),
    // and a stop an earlier lock had frozen that is now inside the middle.
    const inside = marks.filter( ( mark ) => !mark.shape && mark.vertex > start && mark.vertex < end );
    if ( this.lock ) {
      this.lock.trip.forEach( ( point ) => {
        const vertex = this.nearest( line , point.lat , point.lon , 0 , line.length - 1 );
        const close = this.metres( point.lat , point.lon , line[ vertex ][ 0 ] , line[ vertex ][ 1 ] ) < 50;
        if ( close && vertex > start && vertex < end && !inside.some( ( mark ) => Math.abs( mark.vertex - vertex ) < 2 ) ) {
          inside.push( { vertex: vertex , shape: false , lat: point.lat , lon: point.lon } );
        }
      } );
      inside.sort( ( a , b ) => a.vertex - b.vertex );
    }
    const point = ( index ) => ( { lat: line[ index ][ 0 ] , lon: line[ index ][ 1 ] } );
    const middle = [ point( start ) , ...inside.map( ( mark ) => ( { lat: mark.lat , lon: mark.lon } ) ) , point( end ) ];
    const stopAt = [ start , ...inside.map( ( mark ) => mark.vertex ) , end ];
    // Nudges inside the middle stay, on their legs of it.
    const shapeAt = marks.filter( ( mark ) => mark.shape ).map( ( mark ) => mark.vertex );
    const shape = [];
    if ( shapeAt.length === this.shape.length ) {
      this.shape.forEach( ( nudge , index ) => {
        const vertex = shapeAt[ index ];
        if ( vertex <= start || vertex >= end ) return;
        const leg = stopAt.filter( ( at ) => at <= vertex ).length - 1;
        shape.push( { lat: nudge.lat , lon: nudge.lon , leg: Math.max( 0 , Math.min( leg , middle.length - 2 ) ) } );
      } );
    }
    const trip = this.lock ? this.lock.trip
      : this.isLoop() ? marks.filter( ( mark ) => !mark.shape ).map( ( mark ) => ( { lat: mark.lat , lon: mark.lon } ) )
      : this.stops.filter( Boolean ).map( ( stop ) => ( { lat: stop.lat , lon: stop.lon } ) );
    const lock = {
      id: this.newLockId() , coords: line , seconds: this.chosen().seconds , start: start , end: end , middle: middle , trip: trip,
    };
    this.picking = null;
    this.change( "undo.plan_lock" , ( state ) => { state.lock = lock; state.shape = shape; state.lockPending = null; state.weave = null; } );
    return true;
  },

  // --- switching at forks ----------------------------------------------------
  //
  // The toolbar's Forks mode: wherever another of the routes offered leaves
  // the chosen one (where they part, or cross), a signpost pin. Picking a
  // route there drives the chosen one up to the fork, then that one to the
  // end: a woven route, { id , coords , seconds , trip , pool }. pool is the
  // routes offered when weaving began (their lines and times), kept so the
  // switching can go on; the server only measures the woven line (POST
  // /api/route follow). A nudge, a lock or a change of stops ends the weave.

  FORK_NEAR_M: 12,
  FORK_STEP_M: 8,
  // A route must stay this far off the chosen one to count as leaving it.
  FORK_AWAY_M: 120,
  FORK_MAX: 40,

  setEditMode( mode ) {
    this.editMode = mode === "forks" ? "forks" : "nudge";
    try { window.localStorage.setItem( "plan-edit-mode" , this.editMode ); } catch ( _error ) { /* private mode */ }
    Dom.all( "input[name=plan-edit-mode]" ).forEach( ( radio ) => { radio.checked = radio.value === this.editMode; } );
    if ( this.editMode === "forks" && this.isLockPicking() ) this.picking = null;
    this.showHint();
    this.drawShaping();
    this.renderShapeTools();
  },

  isForks() {
    return this.editMode === "forks";
  },

  lineOf( route ) {
    const out = [];
    route.runs.forEach( ( run ) => run.coords.forEach( ( point , index ) => {
      const last = out[ out.length - 1 ];
      if ( index === 0 && last && last[ 0 ] === point[ 0 ] && last[ 1 ] === point[ 1 ] ) return;
      out.push( [ Number( point[ 0 ] ) , Number( point[ 1 ] ) ] );
    } ) );
    return out;
  },

  // The routes to switch onto: the weave's own, or the routes offered now.
  forkPool() {
    if ( this.weave ) return this.weave.pool || [];
    return this.choices().map( ( route , index ) => ( {
      n: index + 1 , line: this.lineOf( route ) , seconds: route.seconds , main_seconds: route.main_seconds , meters: route.meters,
    } ) );
  },

  // The line every few metres, each point with the segment it is on.
  densify( line , step ) {
    const out = [];
    for ( let k = 0 ; k < line.length - 1 ; k++ ) {
      const [ lat1 , lon1 ] = line[ k ];
      const [ lat2 , lon2 ] = line[ k + 1 ];
      const pieces = Math.max( 1 , Math.ceil( this.metres( lat1 , lon1 , lat2 , lon2 ) / step ) );
      for ( let i = 0 ; i < pieces ; i++ ) {
        const t = i / pieces;
        out.push( { lat: lat1 + ( lat2 - lat1 ) * t , lon: lon1 + ( lon2 - lon1 ) * t , seg: k } );
      }
    }
    if ( line.length ) {
      const last = line[ line.length - 1 ];
      out.push( { lat: last[ 0 ] , lon: last[ 1 ] , seg: Math.max( 0 , line.length - 2 ) , end: true } );
    }
    return out;
  },

  // Metres along a line, at each vertex.
  along( line ) {
    const out = [ 0 ];
    for ( let k = 1 ; k < line.length ; k++ ) out.push( out[ k - 1 ] + this.metres( line[ k - 1 ][ 0 ] , line[ k - 1 ][ 1 ] , line[ k ][ 0 ] , line[ k ][ 1 ] ) );
    return out;
  },

  // Where each route in pool leaves line: [{ lat , lon , options: [{ entry , coords , seconds , main_seconds }] }].
  forks( line , pool , current ) {
    if ( line.length < 2 || !pool.length ) return [];
    const CELL = 0.0002;
    const grid = new Map();
    const cellKey = ( lat , lon ) => Math.floor( lat / CELL ) + ":" + Math.floor( lon / CELL );
    this.densify( line , this.FORK_STEP_M ).forEach( ( point ) => {
      const key = cellKey( point.lat , point.lon );
      if ( !grid.has( key ) ) grid.set( key , [] );
      grid.get( key ).push( point );
    } );
    const onLine = ( lat , lon ) => {
      const r = Math.floor( lat / CELL );
      const c = Math.floor( lon / CELL );
      let best = null;
      let bestM = this.FORK_NEAR_M;
      for ( let dr = -1 ; dr <= 1 ; dr++ ) for ( let dc = -1 ; dc <= 1 ; dc++ ) {
        ( grid.get( ( r + dr ) + ":" + ( c + dc ) ) || [] ).forEach( ( point ) => {
          const m = this.metres( lat , lon , point.lat , point.lon );
          if ( m < bestM ) { bestM = m; best = point; }
        } );
      }
      return best;
    };
    const lineAlong = this.along( line );
    const lineLength = lineAlong[ lineAlong.length - 1 ] || 1;
    const away = Math.ceil( this.FORK_AWAY_M / this.FORK_STEP_M );
    const found = [];
    pool.forEach( ( entry ) => {
      const other = entry.line;
      if ( other.length < 2 ) return;
      const otherAlong = this.along( other );
      const otherLength = otherAlong[ otherAlong.length - 1 ] || 1;
      const dense = this.densify( other , this.FORK_STEP_M );
      const on = dense.map( ( point ) => onLine( point.lat , point.lon ) );
      for ( let i = 0 ; i < dense.length - 1 ; i++ ) {
        if ( !on[ i ] || on[ i + 1 ] ) continue;
        // It has to stay off for a while: not a wobble beside the same road.
        let off = 0;
        while ( i + 1 + off < dense.length && !on[ i + 1 + off ] && off < away ) off++;
        if ( off < away && i + 1 + off < dense.length ) continue;
        const at = dense[ i ];
        const mine = on[ i ];
        const otherLeft = otherLength - ( otherAlong[ at.seg ] + this.metres( other[ at.seg ][ 0 ] , other[ at.seg ][ 1 ] , at.lat , at.lon ) );
        if ( otherLeft < 150 ) continue;
        const mineDone = lineAlong[ mine.seg ] + this.metres( line[ mine.seg ][ 0 ] , line[ mine.seg ][ 1 ] , mine.lat , mine.lon );
        const coords = line.slice( 0 , mine.seg + 1 ).concat( [ [ at.lat , at.lon ] ] , other.slice( at.seg + 1 ) );
        const share = mineDone / lineLength;
        const rest = otherLeft / otherLength;
        found.push( {
          lat: at.lat , lon: at.lon,
          option: {
            entry: entry , coords: coords , from: [ at.lat , at.lon ],
            rest: [ [ at.lat , at.lon ] ].concat( other.slice( at.seg + 1 ) ),
            seconds: current.seconds * share + entry.seconds * rest,
            main_seconds: current.main_seconds * share + entry.main_seconds * rest,
          },
        } );
        i += off;
      }
    } );
    // One pin per place, with every route that leaves there.
    const pins = [];
    found.forEach( ( fork ) => {
      const pin = pins.find( ( other ) => this.metres( other.lat , other.lon , fork.lat , fork.lon ) < 40 );
      if ( pin ) {
        if ( !pin.options.some( ( option ) => option.entry === fork.option.entry ) ) pin.options.push( fork.option );
      } else pins.push( { lat: fork.lat , lon: fork.lon , options: [ fork.option ] } );
    } );
    pins.forEach( ( pin ) => pin.options.sort( ( a , b ) => a.entry.n - b.entry.n ) );
    return pins.slice( 0 , this.FORK_MAX );
  },

  // The pins for the chosen route, and what each option there would change.
  forkPins() {
    const chosen = this.chosen();
    if ( !chosen || !this.isForks() || this.lock || this.isLoop() ) return [];
    const line = this.weave ? this.weave.coords : this.routeLine();
    const pool = this.forkPool().filter( ( entry ) => this.weave || entry.n !== this.selected + 1 );
    return this.forks( line , pool , chosen ).map( ( pin ) => ( {
      lat: pin.lat , lon: pin.lon,
      options: pin.options.map( ( option ) => ( {
        label: Format.fill( "plan.fork_take" , { n: option.entry.n } ),
        detail: this.forkDetail( option.seconds - chosen.seconds , option.main_seconds - chosen.main_seconds ),
        preview: { runs: [ { coords: option.rest } ] },
        pick: () => this.switchAt( option ),
      } ) ),
    } ) );
  },

  forkDetail( extra , main ) {
    const time = Math.abs( extra ) < 30 ? I18n.get( "plan.fork_same_time" )
      : Format.fill( extra > 0 ? "plan.fork_slower" : "plan.fork_faster" , { time: Format.duration( Math.abs( extra ) ) } );
    if ( Math.abs( main ) < 30 ) return time;
    return time + " · " + Format.fill( main > 0 ? "plan.fork_more_main" : "plan.fork_less_main" , { time: Format.duration( Math.abs( main ) ) } );
  },

  switchAt( option ) {
    const pool = this.forkPool();
    const trip = this.stops.filter( Boolean ).map( ( stop ) => ( { lat: stop.lat , lon: stop.lon } ) );
    const weave = { id: this.newLockId() , coords: option.coords , seconds: Math.round( option.seconds ) , trip: trip , pool: pool };
    RouteMap.map.closePopup();
    RouteMap.highlight( null );
    this.change( "undo.plan_weave" , ( state ) => { state.weave = weave; state.shape = []; state.lock = null; state.lockPending = null; } );
  },

  // The weave's routes drawn faint behind it, like the routes offered.
  poolRoutes() {
    if ( !this.weave ) return [];
    return ( this.weave.pool || [] ).map( ( entry ) => ( { runs: [ { coords: entry.line } ] , compare: false , pool: true } ) );
  },

  resetShape() {
    this.picking = null;
    if ( !this.shape.length && !this.lock && !this.lockPending && !this.weave && !this.ringEdits() ) {
      this.showHint();
      this.drawShaping();
      this.renderShapeTools();
      return;
    }
    this.change( "undo.plan_reset" , ( state ) => this.unshape( state ) );
  },

  // How many ring points the loop has had dropped or moved.
  ringEdits() {
    return this.isLoop() ? ( this.options.loop.edits || [] ).length : 0;
  },

  renderShapeTools() {
    const lockButton = Dom.get( "plan-lock" );
    const picking = this.isLockPicking();
    lockButton.classList.toggle( "active" , picking );
    lockButton.setAttribute( "aria-pressed" , picking ? "true" : "false" );
    Dom.text( lockButton.querySelector( "span" ) , I18n.get( picking ? "plan.lock_cancel" : "plan.lock_point" ) );
    lockButton.disabled = !this.chosen();
    Dom.show( Dom.get( "plan-reset-shape" ) , this.shape.length > 0 || this.lock !== null || this.lockPending !== null || this.weave !== null || this.ringEdits() > 0 );
    Dom.all( "input[name=plan-edit-mode]" ).forEach( ( radio ) => { radio.disabled = !this.chosen() || this.isLoop(); } );
    const parts = [];
    if ( this.shape.length ) parts.push( this.shape.length === 1 ? I18n.get( "plan.nudge_one" ) : Format.fill( "plan.nudge_count" , { n: this.shape.length } ) );
    if ( this.lock ) parts.push( I18n.get( "plan.nudge_locked" ) );
    if ( this.weave ) parts.push( I18n.get( "plan.woven" ) );
    if ( this.ringEdits() ) parts.push( this.ringEdits() === 1 ? I18n.get( "plan.ring_edit_one" ) : Format.fill( "plan.ring_edit_count" , { n: this.ringEdits() } ) );
    const status = Dom.get( "plan-shape-status" );
    Dom.text( status , parts.join( " · " ) );
    Dom.show( status , parts.length > 0 );
  },

  // The toolbar's undo and redo arrows follow the history.
  renderHistory() {
    Dom.get( "plan-undo" ).disabled = !Undo.canUndo();
    Dom.get( "plan-redo" ).disabled = !Undo.canRedo();
  },

  select( index ) {
    if ( index === this.selected ) return;
    this.selected = index;
    this.remember();
    this.draw();
    this.renderResult( this.result );
  },

  // --- the result card -------------------------------------------------------------

  stat( labelKey , value , extra ) {
    return Dom.el( "div" , { class: "col" , children: [
      Dom.el( "div" , { class: "small text-body-secondary" , text: I18n.get( labelKey ) } ),
      Dom.el( "div" , { class: "fs-5 fw-semibold" , text: value } ),
      extra || null,
    ].filter( Boolean ) } );
  },

  sourceLabel( id , name ) {
    return id === "ours" ? I18n.get( "plan.source_ours" ) : name || id;
  },

  routeTitle( route ) {
    const kind = I18n.get( route.kind === "fastest" ? "plan.kind_fastest" : "plan.kind_backroads" );
    const many = this.usableSources().length > 1 || route.source !== "ours";
    return many ? Format.fill( "plan.kind_from" , { kind: kind , source: this.sourceLabel( route.source , route.source_name ) } ) : kind;
  },

  routeBlock( route , best ) {
    const share = route.seconds > 0 ? ( 100 * route.main_seconds ) / route.seconds : 0;
    const bar = Dom.el( "div" , {
      class: "progress plan-share mt-1",
      attrs: { role: "progressbar" , "aria-valuemin": "0" , "aria-valuemax": "100" , "aria-valuenow": String( Math.round( share ) ) , "aria-label": I18n.get( "plan.on_main" ) },
      children: [ Dom.el( "div" , { class: "progress-bar plan-share-main" } ) ],
    } );
    bar.firstChild.style.width = share.toFixed( 1 ) + "%";
    const children = [
      Dom.el( "h4" , { class: "h6 mb-2 d-flex align-items-center gap-2" , children: [
        Dom.el( "span" , { text: this.routeTitle( route ) } ),
        best ? Dom.el( "span" , { class: "badge text-bg-success" , text: I18n.get( "plan.best_badge" ) } ) : null,
      ].filter( Boolean ) } ),
      Dom.el( "div" , { class: "row row-cols-3 g-2" , children: [
        this.stat( "plan.time" , Format.duration( route.seconds ) ),
        this.stat( "plan.distance" , Format.distance( route.meters ) ),
        this.stat( "plan.on_main" , Format.duration( route.main_seconds ) , Dom.el( "div" , { class: "small text-body-secondary" , text: Format.share( route.main_seconds , route.seconds ) } ) ),
      ] } ),
      bar,
    ];
    if ( route.delay_seconds >= 30 ) {
      children.push( Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: Format.fill( "plan.delay" , { time: Format.duration( route.delay_seconds ) } ) } ) );
    }
    if ( route.unpaved_meters > 0 ) {
      children.push( Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: Format.fill( "plan.unpaved" , { distance: Format.distance( route.unpaved_meters ) } ) } ) );
    }
    if ( route.estimated ) {
      children.push( Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: Format.fill( "plan.estimated" , { source: this.sourceLabel( route.source , route.source_name ) } ) } ) );
    }
    if ( route.off_map_meters >= 500 ) {
      children.push( Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: Format.fill( "plan.off_map" , { distance: Format.distance( route.off_map_meters ) } ) } ) );
    }
    if ( route.also && route.also.length ) {
      const names = route.also.map( ( id ) => {
        const known = this.sourceList().find( ( source ) => source.id === id );
        return this.sourceLabel( id , known ? known.name : id );
      } );
      children.push( Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: Format.fill( "plan.also_found" , { sources: names.join( ", " ) } ) } ) );
    }
    return Dom.el( "div" , { class: "mb-3" , children: children } );
  },

  // One of several routes, in one line: a button that shows it on the map
  // (and its details below the list).
  choiceButton( route , index ) {
    const on = index === this.selected;
    const share = route.seconds > 0 ? ( 100 * route.main_seconds ) / route.seconds : 0;
    const bar = Dom.el( "div" , {
      class: "progress plan-share mt-1",
      attrs: { role: "presentation" },
      children: [ Dom.el( "div" , { class: "progress-bar plan-share-main" } ) ],
    } );
    bar.firstChild.style.width = share.toFixed( 1 ) + "%";
    return Dom.el( "button" , {
      class: "list-group-item list-group-item-action text-start py-2" + ( on ? " bg-primary-subtle border-primary" : "" ),
      attrs: { type: "button" , "aria-pressed": on ? "true" : "false" , "data-key": "route-" + index },
      children: [
        Dom.el( "div" , { class: "d-flex align-items-center gap-2" , children: [
          Dom.el( "span" , { class: "fw-semibold small me-auto" , text: Format.fill( "plan.choice_name" , { n: index + 1 , source: this.sourceLabel( route.source , route.source_name ) } ) } ),
          index === 0 ? Dom.el( "span" , { class: "badge text-bg-success" , text: I18n.get( "plan.best_badge" ) } ) : null,
        ].filter( Boolean ) } ),
        Dom.el( "div" , { class: "small text-body-secondary" , text: [
          Format.duration( route.seconds ),
          Format.distance( route.meters ),
          Format.fill( "plan.choice_main" , { share: Format.share( route.main_seconds , route.seconds ) } ),
        ].join( " · " ) } ),
        bar,
      ],
      on: {
        click: () => this.select( index ),
        mouseenter: () => { if ( index !== this.selected ) RouteMap.highlight( route ); },
        mouseleave: () => RouteMap.highlight( null ),
        focus: () => { if ( index !== this.selected ) RouteMap.highlight( route ); },
        blur: () => RouteMap.highlight( null ),
      },
    } );
  },

  // How the chosen route compares with the fastest: longer or shorter, and
  // more or less on main roads (another engine's route can be either).
  tradeText( extra , saved ) {
    if ( Math.abs( extra ) < 30 && Math.abs( saved ) < 30 ) return I18n.get( "plan.trade_same" );
    const length = Math.abs( extra ) < 30 ? I18n.get( "plan.trade_as_long" )
      : Format.fill( extra > 0 ? "plan.trade_longer" : "plan.trade_shorter" , { time: Format.duration( Math.abs( extra ) ) } );
    const main = Math.abs( saved ) < 30 ? I18n.get( "plan.trade_main_same" )
      : Format.fill( saved > 0 ? "plan.trade_main_less" : "plan.trade_main_more" , { time: Format.duration( Math.abs( saved ) ) } );
    return Format.fill( "plan.trade" , { length: length , main: main } );
  },

  renderResult( result ) {
    const problems = ( result.problems || [] ).map( ( problem ) => Dom.el( "div" , {
      text: Format.fill( "plan.problem" , { source: this.sourceLabel( problem.source , problem.source_name ) , message: problem.message } ),
    } ) );
    if ( result.off_map ) problems.unshift( Dom.el( "div" , { class: "fw-semibold" , text: I18n.get( "plan.off_map_planned" ) } ) );
    Dom.swap( Dom.get( "plan-problems" ) , ...problems );
    Dom.show( Dom.get( "plan-problems" ) , problems.length > 0 );
    this.renderAlternativesNote();

    const choices = this.choices();
    const chosen = this.chosen();
    const fastest = this.comparison();
    const blocks = [];
    if ( choices.length > 1 ) {
      blocks.push( Dom.el( "p" , { class: "small text-body-secondary mb-2" , text: Format.fill( "plan.routes_count" , { n: choices.length } ) } ) );
      blocks.push( Dom.el( "div" , { class: "list-group mb-3" , children: choices.map( ( route , index ) => this.choiceButton( route , index ) ) } ) );
    }
    if ( chosen ) blocks.push( this.routeBlock( chosen , choices.length > 1 && this.selected === 0 ) );
    if ( chosen && fastest ) {
      const extra = chosen.seconds - fastest.seconds;
      const saved = fastest.main_seconds - chosen.main_seconds;
      const text = this.tradeText( extra , saved );
      blocks.push( Dom.el( "p" , { class: "small alert alert-light border py-2" , text: text } ) );
      const compareBlock = this.routeBlock( fastest , false );
      compareBlock.classList.add( "opacity-75" );
      blocks.push( compareBlock );
    }
    Dom.swap( Dom.get( "plan-summary" ) , ...blocks );

    const via = ( chosen ? chosen.via : [] ).map( ( road ) => Dom.el( "li" , {
      class: "list-group-item d-flex justify-content-between align-items-start gap-2 px-0",
      children: [
        Dom.el( "span" , { class: "me-auto" , text: road.name || I18n.get( "plan.unnamed" ) } ),
        road.main ? Dom.el( "span" , { class: "badge plan-badge-main" , text: I18n.get( "plan.main_badge" ) } ) : null,
        Dom.el( "span" , { class: "text-body-secondary text-nowrap" , text: Format.distance( road.meters ) } ),
      ].filter( Boolean ),
    } ) );
    Dom.swap( Dom.get( "plan-via" ) , ...via );
    this.renderShapeTools();
    Dom.show( Dom.get( "plan-result" ) , true );
  },

  // --- save and export -------------------------------------------------------------

  defaultName() {
    const short = ( stop ) => stop.label.split( "," )[ 0 ];
    if ( this.isLoop() ) return Format.fill( "plan.save_name_loop" , { center: short( this.loopPoints[ 0 ] ) } ).slice( 0 , 80 );
    const set = this.stops.filter( Boolean );
    return Format.fill( "plan.save_name_default" , { from: short( set[ 0 ] ) , to: short( set[ set.length - 1 ] ) } ).slice( 0 , 80 );
  },

  async saveRoute() {
    const route = this.chosen();
    if ( !route ) return;
    const stops = this.points().filter( Boolean );
    const loop = this.isLoop() ? this.loopSpec() : null;
    let made = null;
    const answer = await Dom.form( {
      title: I18n.get( "plan.save_title" ),
      fields: [ { name: "name" , label: I18n.get( "plan.save_name" ) , value: this.defaultName() , required: true , maxLength: 80 } ],
      save: async ( values ) => {
        made = await Api.post( "/api/saved-routes" , {
          name: values.name,
          waypoints: stops.map( ( stop ) => ( { lat: stop.lat , lon: stop.lon , label: stop.label.slice( 0 , 200 ) } ) ),
          options: {
            backroads: this.options.backroads,
            penalty: this.options.penalty,
            road_rules: this.options.road_rules,
            road_factors: this.options.road_factors,
            sources: this.usableSources(),
            combine: this.options.combine || "best",
            alternatives: this.options.alternatives || 1,
            pick: { source: route.source , rank: route.rank || 0 },
            loop: loop,
            shape: this.shape.map( ( point ) => ( { lat: point.lat , lon: point.lon , leg: point.leg } ) ),
            follow: this.weave ? { coords: this.weave.coords , seconds: this.weave.seconds , trip: this.weave.trip } : null,
            lock: this.lock ? {
              keep: { coords: this.lock.coords , seconds: this.lock.seconds , start: this.lock.start , end: this.lock.end , trip: this.lock.trip },
              middle: this.lock.middle,
            } : null,
          },
          summary: { seconds: route.seconds , meters: route.meters , main_seconds: route.main_seconds , main_meters: route.main_meters },
        } );
      },
    } );
    if ( answer === null || !made ) return;
    this.rememberPlaces( stops );
    const id = made.id;
    // What is on screen is now this saved route: its link is the address.
    const link = this.linkOf( made , null );
    this.link = link;
    this.renderLink();
    // Undoing the save lets go of its link, if it is still the one on screen.
    const forget = () => {
      if ( !this.link || this.link.token !== link.token ) return;
      this.link = null;
      this.renderLink();
    };
    Undo.record( {
      label: I18n.get( "undo.route_save" ),
      toast: true,
      undo: async () => { await Api.post( "/api/saved-routes/" + id + "/delete" ); forget(); SavedRoutes.refresh(); },
      redo: async () => { await Api.post( "/api/saved-routes/" + id + "/restore" ); SavedRoutes.refresh(); },
    } );
    SavedRoutes.refresh();
  },

  exportAs( format ) {
    const route = this.chosen();
    if ( !route ) return;
    const stops = this.points().filter( Boolean );
    Export.run( format , this.defaultName() || I18n.get( "plan.gpx_name" ) , stops , route );
  },
};
