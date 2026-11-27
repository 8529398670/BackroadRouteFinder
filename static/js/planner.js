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
const Planner = {
  stops: [ null , null ],
  loopPoints: [ null , null ],
  options: {
    backroads: true , penalty: 8 , road_rules: {} , road_factors: {} , compare_fastest: true , sources: [ "ours" ] , combine: "best" , alternatives: 5,
    loop: { on: false , radius: 20 , unit: "mi" , start: "center" , direction: "cw" , points: 6 , reuse: 3 },
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
  // A saved route's pick ({source, rank}), chosen once its plan comes back.
  pendingPick: null,
  ALTERNATIVES: [ 1 , 2 , 3 , 4 , 5 , 6 , 7 , 8 , 9 , 10 ],
  picking: null,
  turn: 0,
  status: null,
  planTimer: null,

  async init( me ) {
    this.me = me;
    RouteMap.init( "plan-map" , {
      click: ( lat , lon ) => this.onMapClick( lat , lon ),
      drag: ( index , lat , lon ) => this.setStop( index , this.pointStop( lat , lon ) ),
    } );
    window.addEventListener( "nav" , ( event ) => {
      if ( event.detail.screen === "plan" ) window.setTimeout( () => RouteMap.resize() , 0 );
    } );
    window.addEventListener( "resize" , () => RouteMap.resize() );

    Dom.get( "plan-add-stop" ).addEventListener( "click" , () => this.addStop() );
    Dom.get( "plan-reverse" ).addEventListener( "click" , () => this.reverse() );
    Dom.get( "plan-clear" ).addEventListener( "click" , () => this.clear() );
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
    Dom.get( "plan-penalty" ).addEventListener( "input" , ( event ) => this.setOption( "penalty" , Number( event.target.value ) , "penalty" ) );
    Dom.all( "#plan-presets [data-penalty]" ).forEach( ( button ) => {
      button.addEventListener( "click" , () => this.setOption( "penalty" , Number( button.getAttribute( "data-penalty" ) ) ) );
    } );
    const many = Dom.get( "plan-alternatives" );
    this.ALTERNATIVES.forEach( ( n ) => many.appendChild( Dom.el( "option" , { text: String( n ) , attrs: { value: String( n ) } } ) ) );
    many.addEventListener( "change" , () => this.setOption( "alternatives" , Number( many.value ) ) );
    Dom.onPress( Dom.get( "plan-save-prefs" ) , () => this.savePrefs() );
    Dom.onPress( Dom.get( "plan-save" ) , () => this.saveRoute() );
    Dom.all( "#plan-export-menu [data-export]" ).forEach( ( item ) => {
      item.addEventListener( "click" , () => this.exportAs( item.dataset.export ) );
    } );

    const [ status , prefs ] = await Promise.all( [
      Api.request( "/api/routing/status" ),
      Api.request( "/api/planner/prefs" ),
    ] );
    this.status = status;
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
      loop: Object.assign( {} , options.loop || {} ),
      sources: ( options.sources || [] ).slice(),
    } );
  },

  copyPoints( list ) {
    return list.map( ( stop ) => ( stop ? Object.assign( {} , stop ) : null ) );
  },

  snapshot() {
    return { stops: this.copyPoints( this.stops ) , loopPoints: this.copyPoints( this.loopPoints ) , options: this.copyOptions( this.options ) };
  },

  apply( state , fit ) {
    this.stops = this.copyPoints( state.stops );
    this.loopPoints = this.copyPoints( state.loopPoints );
    this.options = this.copyOptions( state.options );
    this.renderStops();
    this.renderOptions();
    this.schedulePlan( fit );
  },

  change( labelKey , mutate , merge , fit ) {
    const before = this.snapshot();
    const after = this.snapshot();
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

  setStop( index , stop , fit ) {
    const loop = this.isLoop();
    this.change( "undo.plan_stops" , ( state ) => { ( loop ? state.loopPoints : state.stops )[ index ] = stop; } , null , fit );
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

  setLoop( name , value , merge , fit ) {
    if ( this.options.loop[ name ] === value ) return;
    this.picking = null;
    this.change( "undo.plan_options" , ( state ) => { state.options.loop[ name ] = value; } , merge || null , fit );
  },

  onMapClick( lat , lon ) {
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
  },

  addStop() {
    if ( this.isLoop() ) return;
    this.change( "undo.plan_stops" , ( state ) => { state.stops.splice( state.stops.length - 1 , 0 , null ); } );
    this.picking = this.stops.length - 2;
    this.showHint();
  },

  removeStop( index ) {
    this.change( "undo.plan_stops" , ( state ) => { state.stops.splice( index , 1 ); } );
  },

  reverse() {
    if ( this.isLoop() ) return;
    this.change( "undo.plan_stops" , ( state ) => { state.stops.reverse(); } );
  },

  clear() {
    if ( this.points().every( ( stop ) => stop === null ) ) return;
    const loop = this.isLoop();
    this.change( "undo.plan_stops" , ( state ) => {
      if ( loop ) state.loopPoints = [ null , null ];
      else state.stops = [ null , null ];
    } );
  },

  // Opening a saved route (saved_routes.js): its stops and options, undoable
  // like any other change, and the map moves to show it.
  open( saved ) {
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
      } else {
        state.stops = points;
        state.options.loop.on = false;
      }
      if ( saved.options.sources ) state.options.sources = saved.options.sources.slice();
      if ( saved.options.combine ) state.options.combine = saved.options.combine;
      if ( saved.options.alternatives ) state.options.alternatives = saved.options.alternatives;
    } , null , true );
    this.pendingPick = saved.options.pick || null;
    Nav.go( "/plan" );
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
    input.addEventListener( "input" , () => this.showSearching( input , menu ) );
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

  showSearching( input , menu ) {
    const query = input.value.trim();
    if ( query.length < 2 || ( this.status && !this.status.place_search ) ) return;
    Dom.swap( menu , Dom.el( "div" , { class: "list-group-item small text-body-secondary d-flex align-items-center gap-2" , children: [
      Dom.el( "span" , { class: "spinner-border spinner-border-sm text-primary" , attrs: { "aria-hidden": "true" } } ),
      Dom.el( "span" , { text: Format.fill( "plan.searching" , { query: query } ) } ),
    ] } ) );
    Dom.show( menu , true );
  },

  async search( index , input , menu ) {
    const query = input.value.trim();
    if ( query.length < 2 ) {
      Dom.show( menu , false );
      return;
    }
    if ( this.status && !this.status.place_search ) {
      Dom.swap( menu , Dom.el( "div" , { class: "list-group-item small text-body-secondary" , text: I18n.get( "plan.search_off" ) } ) );
      Dom.show( menu , true );
      return;
    }
    let places;
    try {
      places = ( await Api.request( "/api/geocode?q=" + encodeURIComponent( query ) ) ).places;
    } catch ( error ) {
      if ( input.value.trim() === query ) Dom.show( menu , false );
      App.showError( error.message );
      return;
    }
    if ( input.value.trim() !== query ) return;
    const rows = places.length ? places.map( ( place ) => Dom.el( "button" , {
      class: "list-group-item list-group-item-action small",
      attrs: { type: "button" , role: "option" },
      text: place.name,
      on: { click: () => {
        Dom.show( menu , false );
        this.setStop( index , { lat: place.lat , lon: place.lon , label: place.name } , true );
      } },
    } ) ) : [ Dom.el( "div" , { class: "list-group-item small text-body-secondary" , text: I18n.get( "plan.search_none" ) } ) ];
    Dom.swap( menu , ...rows );
    Dom.show( menu , true );
  },

  showHint() {
    let text;
    const points = this.loopPoints;
    if ( this.picking !== null ) text = Format.fill( "plan.hint_pick" , { stop: this.stopName( this.picking ) } );
    else if ( this.isLoop() ) {
      if ( points[ 0 ] === null ) text = I18n.get( "plan.hint_loop_center" );
      else if ( this.loopUsesStart() && points[ 1 ] === null ) text = I18n.get( "plan.hint_loop_start" );
      else text = I18n.get( "plan.hint_loop_done" );
    } else if ( this.stops[ 0 ] === null ) text = I18n.get( "plan.hint_start" );
    else if ( this.stops.some( ( stop ) => stop === null ) ) text = I18n.get( "plan.hint_end" );
    else text = I18n.get( "plan.hint_done" );
    Dom.text( Dom.get( "plan-hint" ) , text );
    Dom.get( "plan-map" ).classList.toggle( "plan-map-picking" , this.picking !== null );
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
    Dom.get( "plan-alternatives" ).value = String( this.options.alternatives || 1 );
  },

  async savePrefs() {
    const options = this.options;
    await Api.post( "/api/planner/prefs" , {
      backroads: options.backroads , penalty: options.penalty , road_rules: options.road_rules , road_factors: options.road_factors,
      compare_fastest: options.compare_fastest , sources: options.sources , combine: options.combine || "best",
      alternatives: options.alternatives || 1,
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
    if ( loop ) body.loop = this.loopSpec();
    const key = JSON.stringify( body );
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
      App.showError( error.message );
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
    this.show( key , result , selected , fit );
  },

  show( key , result , selected , fit ) {
    this.cacheKey = key;
    this.result = result;
    this.selected = selected;
    this.remember();
    this.draw();
    if ( fit || !this.shownOnce ) RouteMap.fitTo( result.routes , this.points() );
    this.shownOnce = true;
    this.renderResult( result );
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
    RouteMap.drawRoutes( [ chosen , ...this.result.routes.filter( ( route ) => route !== chosen ) ] , ( route ) => {
      const index = choices.indexOf( route );
      if ( index >= 0 ) this.select( index );
    } );
    if ( this.isLoop() && this.result.ring && this.result.ring.length ) {
      const loop = this.options.loop;
      RouteMap.drawRing( this.loopPoints[ 0 ] , this.result.ring , loop.unit === "mi" ? loop.radius * 1609.344 : null );
    }
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
    Dom.swap( Dom.get( "plan-problems" ) , ...problems );
    Dom.show( Dom.get( "plan-problems" ) , problems.length > 0 );

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
          },
          summary: { seconds: route.seconds , meters: route.meters , main_seconds: route.main_seconds , main_meters: route.main_meters },
        } );
      },
    } );
    if ( answer === null || !made ) return;
    const id = made.id;
    Undo.record( {
      label: I18n.get( "undo.route_save" ),
      toast: true,
      undo: async () => { await Api.post( "/api/saved-routes/" + id + "/delete" ); SavedRoutes.refresh(); },
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
