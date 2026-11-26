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
  options: { backroads: true , penalty: 8 , secondary_is_main: false , allow_unpaved: false , compare_fastest: true },
  result: null,
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

  // --- the map's state ------------------------------------------------------

  showStatus() {
    const box = Dom.get( "plan-map-status" );
    const state = this.status ? this.status.state : "missing";
    let text = "";
    if ( state === "missing" ) text = I18n.get( "plan.map_missing" );
    else if ( state === "loading" ) text = I18n.get( "plan.map_loading" );
    else if ( state === "error" ) text = Format.fill( "plan.map_error" , { error: this.status.error } );
    Dom.text( box , text );
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
    const body = {
      waypoints: this.stops.filter( Boolean ).map( ( stop ) => ( { lat: stop.lat , lon: stop.lon } ) ),
      penalty: options.backroads ? options.penalty : 1,
      secondary_is_main: options.secondary_is_main,
      allow_unpaved: options.allow_unpaved,
      compare_fastest: options.backroads && options.compare_fastest,
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
    RouteMap.drawRoutes( result.routes );
    if ( fit || !this.shownOnce ) RouteMap.fitTo( result.routes , this.stops );
    this.shownOnce = true;
    this.renderResult( result );
  },

  // --- the result card -------------------------------------------------------------

  stat( labelKey , value , extra ) {
    return Dom.el( "div" , { class: "col" , children: [
      Dom.el( "div" , { class: "small text-body-secondary" , text: I18n.get( labelKey ) } ),
      Dom.el( "div" , { class: "fs-5 fw-semibold" , text: value } ),
      extra || null,
    ].filter( Boolean ) } );
  },

  routeBlock( route ) {
    const share = route.seconds > 0 ? ( 100 * route.main_seconds ) / route.seconds : 0;
    const bar = Dom.el( "div" , {
      class: "progress plan-share mt-1",
      attrs: { role: "progressbar" , "aria-valuemin": "0" , "aria-valuemax": "100" , "aria-valuenow": String( Math.round( share ) ) , "aria-label": I18n.get( "plan.on_main" ) },
      children: [ Dom.el( "div" , { class: "progress-bar plan-share-main" } ) ],
    } );
    bar.firstChild.style.width = share.toFixed( 1 ) + "%";
    const children = [
      Dom.el( "h4" , { class: "h6 mb-2" , text: I18n.get( route.kind === "fastest" ? "plan.kind_fastest" : "plan.kind_backroads" ) } ),
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
    return Dom.el( "div" , { class: "mb-3" , children: children } );
  },

  renderResult( result ) {
    const blocks = result.routes.map( ( route ) => this.routeBlock( route ) );
    if ( result.routes.length === 2 ) {
      const chosen = result.routes[ 0 ];
      const fastest = result.routes[ 1 ];
      const extra = chosen.seconds - fastest.seconds;
      const saved = fastest.main_seconds - chosen.main_seconds;
      const text = Math.abs( extra ) < 30 && Math.abs( saved ) < 30
        ? I18n.get( "plan.trade_same" )
        : Format.fill( "plan.trade" , { extra: Format.duration( Math.max( 0 , extra ) ) , saved: Format.duration( Math.max( 0 , saved ) ) } );
      blocks.splice( 1 , 0 , Dom.el( "p" , { class: "small alert alert-light border py-2" , text: text } ) );
      blocks[ 2 ].classList.add( "opacity-75" );
    }
    Dom.swap( Dom.get( "plan-summary" ) , ...blocks );

    const via = result.routes[ 0 ].via.map( ( road ) => Dom.el( "li" , {
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
    if ( !this.result ) return;
    const route = this.result.routes[ 0 ];
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
    if ( !this.result ) return;
    const stops = this.stops.filter( Boolean );
    Gpx.download( this.defaultName() || I18n.get( "plan.gpx_name" ) , stops , this.result.routes[ 0 ] );
  },
};
