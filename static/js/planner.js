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
const Planner = {
  stops: [ null , null ],
  options: { backroads: true , penalty: 8 , secondary_is_main: false , allow_unpaved: false , compare_fastest: true , sources: [ "ours" ] , combine: "best" },
  result: null,
  // Which of the result's routes is shown on the map, saved and exported.
  selected: 0,
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
    Dom.get( "plan-secondary" ).addEventListener( "change" , ( event ) => this.setOption( "secondary_is_main" , event.target.checked ) );
    Dom.get( "plan-unpaved" ).addEventListener( "change" , ( event ) => this.setOption( "allow_unpaved" , event.target.checked ) );
    Dom.get( "plan-compare" ).addEventListener( "change" , ( event ) => this.setOption( "compare_fastest" , event.target.checked ) );
    Dom.get( "plan-penalty" ).addEventListener( "input" , ( event ) => this.setOption( "penalty" , Number( event.target.value ) , "penalty" ) );
    Dom.all( "#plan-presets [data-penalty]" ).forEach( ( button ) => {
      button.addEventListener( "click" , () => this.setOption( "penalty" , Number( button.getAttribute( "data-penalty" ) ) ) );
    } );
    Dom.all( "input[name=plan-combine]" ).forEach( ( radio ) => {
      radio.addEventListener( "change" , () => { if ( radio.checked ) this.setOption( "combine" , radio.value ); } );
    } );
    Dom.onPress( Dom.get( "plan-save-prefs" ) , () => this.savePrefs() );
    Dom.onPress( Dom.get( "plan-save" ) , () => this.saveRoute() );
    Dom.get( "plan-gpx" ).addEventListener( "click" , () => this.exportGpx() );

    const [ status , prefs ] = await Promise.all( [
      Api.request( "/api/routing/status" ),
      Api.request( "/api/planner/prefs" ),
    ] );
    this.status = status;
    this.options = Object.assign( {} , this.options , prefs.prefs );
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

  snapshot() {
    return { stops: this.stops.map( ( stop ) => ( stop ? Object.assign( {} , stop ) : null ) ) , options: Object.assign( {} , this.options ) };
  },

  apply( state , fit ) {
    this.stops = state.stops.map( ( stop ) => ( stop ? Object.assign( {} , stop ) : null ) );
    this.options = Object.assign( {} , state.options );
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

  setStop( index , stop , fit ) {
    this.change( "undo.plan_stops" , ( state ) => { state.stops[ index ] = stop; } , null , fit );
  },

  setOption( name , value , merge ) {
    if ( this.options[ name ] === value ) return;
    this.change( "undo.plan_options" , ( state ) => { state.options[ name ] = value; } , merge || null );
  },

  onMapClick( lat , lon ) {
    let index = this.picking;
    if ( index === null ) {
      index = this.stops.findIndex( ( stop ) => stop === null );
      if ( index < 0 ) index = this.stops.length - 1;
    }
    this.picking = null;
    this.setStop( index , this.pointStop( lat , lon ) );
  },

  addStop() {
    this.change( "undo.plan_stops" , ( state ) => { state.stops.splice( state.stops.length - 1 , 0 , null ); } );
    this.picking = this.stops.length - 2;
    this.showHint();
  },

  removeStop( index ) {
    this.change( "undo.plan_stops" , ( state ) => { state.stops.splice( index , 1 ); } );
  },

  reverse() {
    this.change( "undo.plan_stops" , ( state ) => { state.stops.reverse(); } );
  },

  clear() {
    if ( this.stops.every( ( stop ) => stop === null ) ) return;
    this.change( "undo.plan_stops" , ( state ) => { state.stops = [ null , null ]; } );
  },

  // Opening a saved route (saved_routes.js): its stops and options, undoable
  // like any other change, and the map moves to show it.
  open( saved ) {
    this.change( "undo.plan_stops" , ( state ) => {
      state.stops = saved.waypoints.map( ( point ) => ( { lat: point.lat , lon: point.lon , label: point.label } ) );
      state.options = Object.assign( {} , state.options , {
        backroads: saved.options.backroads,
        penalty: saved.options.penalty,
        secondary_is_main: saved.options.secondary_is_main,
        allow_unpaved: saved.options.allow_unpaved,
      } );
      if ( saved.options.sources ) state.options.sources = saved.options.sources.slice();
      if ( saved.options.combine ) state.options.combine = saved.options.combine;
    } , null , true );
    Nav.go( "/plan" );
  },

  // --- the stops card ----------------------------------------------------------

  stopName( index ) {
    if ( index === 0 ) return I18n.get( "plan.start" );
    if ( index === this.stops.length - 1 ) return I18n.get( "plan.destination" );
    return Format.fill( "plan.stop" , { n: index } );
  },

  renderStops() {
    const items = this.stops.map( ( stop , index ) => this.stopRow( stop , index ) );
    Dom.swap( Dom.get( "plan-stops" ) , ...items );
    RouteMap.drawStops( this.stops );
    this.showHint();
  },

  stopRow( stop , index ) {
    const name = this.stopName( index );
    const kind = index === 0 ? "start" : index === this.stops.length - 1 ? "end" : "via";
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
        on: { click: () => { this.picking = index; this.showHint(); } },
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
      class: "input-group-text plan-stop-badge plan-pin-" + kind,
      text: RouteMap.pinLabel( index , this.stops.length ),
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
    if ( this.picking !== null ) text = Format.fill( "plan.hint_pick" , { stop: this.stopName( this.picking ) } );
    else if ( this.stops[ 0 ] === null ) text = I18n.get( "plan.hint_start" );
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
    Dom.get( "plan-secondary" ).checked = options.secondary_is_main;
    Dom.get( "plan-unpaved" ).checked = options.allow_unpaved;
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
      return Dom.el( "div" , { class: "form-check" , children: [ input , label ] } );
    } );
    Dom.swap( Dom.get( "plan-sources" ) , ...rows );
    const combine = this.options.combine || "best";
    Dom.get( "plan-combine-best" ).checked = combine === "best";
    Dom.get( "plan-combine-all" ).checked = combine === "all";
    Dom.get( "plan-combine" ).disabled = this.usableSources().length < 2;
  },

  async savePrefs() {
    await Api.post( "/api/planner/prefs" , this.options );
    Dom.toast( I18n.get( "plan.prefs_saved" ) , "ok" );
  },

  // --- planning ------------------------------------------------------------------

  ready() {
    return this.stops.length >= 2 && this.stops[ 0 ] !== null && this.stops[ this.stops.length - 1 ] !== null;
  },

  schedulePlan( fit ) {
    window.clearTimeout( this.planTimer );
    this.planTimer = window.setTimeout( () => this.plan( fit ) , 250 );
  },

  async plan( fit ) {
    const turn = ++this.turn;
    if ( !this.ready() || !this.status || this.status.state !== "ready" ) {
      this.result = null;
      RouteMap.clearRoutes();
      Dom.show( Dom.get( "plan-result" ) , false );
      if ( fit ) RouteMap.fitTo( [] , this.stops );
      return;
    }
    const options = this.options;
    const sources = this.usableSources();
    if ( sources.length === 0 ) {
      this.result = null;
      RouteMap.clearRoutes();
      Dom.show( Dom.get( "plan-result" ) , false );
      App.showError( I18n.get( "plan.sources_need_one" ) );
      return;
    }
    const body = {
      waypoints: this.stops.filter( Boolean ).map( ( stop ) => ( { lat: stop.lat , lon: stop.lon } ) ),
      penalty: options.backroads ? options.penalty : 1,
      secondary_is_main: options.secondary_is_main,
      allow_unpaved: options.allow_unpaved,
      compare_fastest: options.backroads && options.compare_fastest,
      sources: sources,
      combine: options.combine || "best",
    };
    const summary = Dom.get( "plan-summary" );
    summary.classList.add( "opacity-50" );
    let result;
    try {
      result = await Api.post( "/api/route" , body );
    } catch ( error ) {
      if ( turn !== this.turn ) return;
      summary.classList.remove( "opacity-50" );
      App.showError( error.message );
      return;
    } finally {
      if ( turn === this.turn ) summary.classList.remove( "opacity-50" );
    }
    if ( turn !== this.turn ) return;
    this.result = result;
    this.selected = 0;
    this.draw();
    if ( fit || !this.shownOnce ) RouteMap.fitTo( result.routes , this.stops );
    this.shownOnce = true;
    this.renderResult( result );
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
    RouteMap.drawRoutes( [ chosen , ...this.result.routes.filter( ( route ) => route !== chosen ) ] );
  },

  select( index ) {
    if ( index === this.selected ) return;
    this.selected = index;
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
          Dom.el( "span" , { class: "fw-semibold small me-auto" , text: this.sourceLabel( route.source , route.source_name ) } ),
          index === 0 ? Dom.el( "span" , { class: "badge text-bg-success" , text: I18n.get( "plan.best_badge" ) } ) : null,
        ].filter( Boolean ) } ),
        Dom.el( "div" , { class: "small text-body-secondary" , text: [
          Format.duration( route.seconds ),
          Format.distance( route.meters ),
          Format.fill( "plan.choice_main" , { share: Format.share( route.main_seconds , route.seconds ) } ),
        ].join( " · " ) } ),
        bar,
      ],
      on: { click: () => this.select( index ) },
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
    const set = this.stops.filter( Boolean );
    const short = ( stop ) => stop.label.split( "," )[ 0 ];
    return Format.fill( "plan.save_name_default" , { from: short( set[ 0 ] ) , to: short( set[ set.length - 1 ] ) } ).slice( 0 , 80 );
  },

  async saveRoute() {
    const route = this.chosen();
    if ( !route ) return;
    const stops = this.stops.filter( Boolean );
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
            secondary_is_main: this.options.secondary_is_main,
            allow_unpaved: this.options.allow_unpaved,
            sources: this.usableSources(),
            combine: this.options.combine || "best",
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

  exportGpx() {
    const route = this.chosen();
    if ( !route ) return;
    const stops = this.stops.filter( Boolean );
    Gpx.download( this.defaultName() || I18n.get( "plan.gpx_name" ) , stops , route );
  },
};
