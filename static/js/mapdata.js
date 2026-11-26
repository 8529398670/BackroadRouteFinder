// The Map data screen (/admin/map-data), owner only.
//
// Pick any US states (or the whole country), download their OpenStreetMap
// extracts from Geofabrik, build one map from them, and switch the server to
// it -- no shell, no restart. The work runs on the server one task at a time
// (app/routing/tasks.py); while it runs this screen asks for its output every
// two seconds.
const MapData = {
  data: null,
  picked: new Set(),
  timer: 0,

  async init( me ) {
    if ( !me.is_owner ) return;
    Dom.show( Dom.get( "map-data-panel" ) , true );
    Dom.get( "mapdata-filter" ).addEventListener( "input" , () => this.renderStates() );
    Dom.get( "mapdata-us" ).addEventListener( "change" , ( event ) => {
      if ( event.target.checked ) this.picked.add( "us" ); else this.picked.delete( "us" );
      this.renderStates();
    } );
    Dom.onPress( Dom.get( "mapdata-select-downloaded" ) , () => {
      this.picked = new Set( this.data.areas.filter( ( a ) => a.downloaded ).map( ( a ) => a.slug ) );
      this.renderStates();
    } );
    Dom.onPress( Dom.get( "mapdata-select-none" ) , () => {
      this.picked.clear();
      this.renderStates();
    } );
    Dom.onPress( Dom.get( "mapdata-download" ) , () => this.download() );
    Dom.onPress( Dom.get( "mapdata-build" ) , () => this.build() );
    Dom.onPress( Dom.get( "mapdata-cancel" ) , () => this.cancel() );
    window.addEventListener( "nav" , () => {
      if ( Nav.current() === "map-data" ) this.load();
    } );
    await this.load();
    // Start with the map in use ticked, so "Build" right away rebuilds it.
    if ( this.data && this.picked.size === 0 ) {
      this.data.loaded.areas.filter( ( slug ) => slug === "us" || this.data.areas.some( ( a ) => a.slug === slug ) )
        .forEach( ( slug ) => this.picked.add( slug ) );
      this.renderStates();
    }
  },

  async load() {
    try {
      this.data = await Api.request( "/api/owner/mapdata" );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    this.render();
  },

  // While something is running (a task, or a map loading) ask again soon.
  schedule() {
    window.clearTimeout( this.timer );
    const d = this.data;
    const busy = d && ( ( d.task && d.task.state === "running" ) || d.loaded.switching || d.loaded.state === "loading" );
    if ( busy ) this.timer = window.setTimeout( () => this.load() , 2000 );
    // The planner hears about a new map once it is in.
    if ( this.wasBusy && !busy && typeof Planner !== "undefined" && Planner.refreshSources ) Planner.refreshSources();
    this.wasBusy = busy;
  },

  render() {
    this.renderLoaded();
    this.renderTask();
    this.renderStates();
    this.renderGraphs();
    this.renderExtracts();
    this.schedule();
  },

  // --- helpers ----------------------------------------------------------------

  bytes( n ) {
    if ( n >= 1e9 ) return ( n / 1e9 ).toFixed( 1 ) + " GB";
    if ( n >= 1e6 ) return Math.round( n / 1e6 ) + " MB";
    if ( n >= 1e3 ) return Math.round( n / 1e3 ) + " KB";
    return n + " B";
  },

  when( iso ) {
    return iso ? new Date( iso ).toLocaleString() : "";
  },

  areaName( slug ) {
    if ( slug === "us" ) return this.data.us.name;
    const area = this.data.areas.find( ( a ) => a.slug === slug );
    return area ? area.name : slug;
  },

  areasLabel( slugs ) {
    return slugs.map( ( slug ) => this.areaName( slug ) ).join( ", " );
  },

  badge( kind , text ) {
    return Dom.el( "span" , { class: "badge text-bg-" + kind , text: text } );
  },

  // --- the map in use ---------------------------------------------------------

  renderLoaded() {
    const l = this.data.loaded;
    const parts = [];
    const states = { ready: "success" , loading: "info" , missing: "warning" , error: "danger" };
    parts.push( Dom.el( "div" , { class: "d-flex flex-wrap align-items-center gap-2 mb-1" , children: [
      Dom.el( "span" , { class: "fw-semibold" , text: this.areasLabel( l.areas ) } ),
      this.badge( states[ l.state ] || "secondary" , I18n.get( "mapdata.state_" + l.state ) ),
      l.switching ? this.badge( "info" , I18n.get( "mapdata.switching" ) ) : null,
    ].filter( Boolean ) } ) );
    if ( l.state === "ready" ) {
      parts.push( Dom.el( "div" , { class: "small text-body-secondary" , text: Format.fill( "mapdata.loaded_detail" , {
        nodes: l.nodes.toLocaleString(), segments: l.segments.toLocaleString(), built: this.when( l.built_at ),
      } ) } ) );
    }
    if ( l.state === "missing" ) parts.push( Dom.el( "div" , { class: "small" , text: I18n.get( "mapdata.loaded_missing" ) } ) );
    if ( l.error ) parts.push( Dom.el( "div" , { class: "small text-danger" , text: l.error } ) );
    Dom.swap( Dom.get( "mapdata-loaded" ) , ...parts );
  },

  // --- the running (or last) task ---------------------------------------------

  renderTask() {
    const task = this.data.task;
    Dom.show( Dom.get( "mapdata-task-card" ) , Boolean( task ) );
    if ( !task ) return;
    Dom.text( Dom.get( "mapdata-task-title" ) , task.title );
    const kinds = { running: "info" , done: "success" , failed: "danger" , cancelled: "secondary" };
    Dom.swap( Dom.get( "mapdata-task-state" ) , this.badge( kinds[ task.state ] || "secondary" ,
      task.state === "running" ? Format.fill( "mapdata.task_step" , { step: task.step + 1 , steps: task.steps } ) : I18n.get( "mapdata.task_" + task.state ) ) );
    Dom.show( Dom.get( "mapdata-cancel" ) , task.state === "running" );
    const log = Dom.get( "mapdata-log" );
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    Dom.text( log , task.lines.join( "\n" ) );
    if ( atEnd ) log.scrollTop = log.scrollHeight;
    // A download prints "(42%)": show it as a bar.
    let percent = -1;
    if ( task.state === "running" ) {
      for ( let i = task.lines.length - 1 ; i >= 0 ; i-- ) {
        const line = task.lines[ i ];
        const match = /\((\d+)%\)\s*$/.exec( line );
        if ( match ) { percent = Number( match[ 1 ] ); break; }
        if ( /^(downloading|reading|building|\$)/.test( line ) ) break;
      }
    }
    Dom.show( Dom.get( "mapdata-progress-wrap" ) , percent >= 0 );
    if ( percent >= 0 ) {
      Dom.get( "mapdata-progress" ).style.width = percent + "%";
      Dom.get( "mapdata-progress-wrap" ).setAttribute( "aria-valuenow" , String( percent ) );
    }
  },

  // --- picking areas ----------------------------------------------------------

  renderStates() {
    if ( !this.data ) return;
    const usPicked = this.picked.has( "us" );
    const us = Dom.get( "mapdata-us" );
    us.checked = usPicked;
    Dom.text( Dom.get( "mapdata-us-note" ) , Format.fill( "mapdata.approx" , { size: this.bytes( this.data.us.approx_mb * 1e6 ) } ) +
      ( this.data.us.downloaded ? " · " + I18n.get( "mapdata.downloaded" ) : "" ) );
    const filter = Dom.get( "mapdata-filter" ).value.trim().toLowerCase();
    const items = this.data.areas.filter( ( a ) => !filter || a.name.toLowerCase().includes( filter ) ).map( ( area ) => {
      const id = "mapdata-state-" + area.slug;
      const input = Dom.el( "input" , { class: "form-check-input" , attrs: { type: "checkbox" , id: id } } );
      input.checked = this.picked.has( area.slug );
      input.disabled = usPicked;
      input.addEventListener( "change" , () => {
        if ( input.checked ) this.picked.add( area.slug ); else this.picked.delete( area.slug );
        this.renderSummary();
      } );
      const size = area.downloaded ? this.bytes( area.downloaded.bytes ) : Format.fill( "mapdata.approx" , { size: this.bytes( area.approx_mb * 1e6 ) } );
      const label = Dom.el( "label" , { class: "form-check-label" , attrs: { for: id } , children: [
        Dom.el( "span" , { text: area.name + " " } ),
        Dom.el( "span" , { class: "small text-body-secondary" , text: size } ),
        area.downloaded ? Dom.el( "i" , { class: "bi bi-check-circle-fill text-success ms-1" , attrs: { title: I18n.get( "mapdata.downloaded" ) , "aria-label": I18n.get( "mapdata.downloaded" ) } } ) : null,
      ].filter( Boolean ) } );
      return Dom.el( "div" , { class: "col" , children: [ Dom.el( "div" , { class: "form-check mb-0" , children: [ input , label ] } ) ] } );
    } );
    Dom.swap( Dom.get( "mapdata-states" ) , ...items );
    this.renderSummary();
  },

  // What is picked: the slugs the server is sent (the whole US wins).
  pickedSlugs() {
    return this.picked.has( "us" ) ? [ "us" ] : [ ...this.picked ].filter( ( s ) => s !== "us" ).sort();
  },

  sizeOf( slug ) {
    const area = slug === "us" ? this.data.us : this.data.areas.find( ( a ) => a.slug === slug );
    return area.downloaded ? area.downloaded.bytes : area.approx_mb * 1e6;
  },

  renderSummary() {
    const slugs = this.pickedSlugs();
    const busy = this.data.task && this.data.task.state === "running";
    const toFetch = slugs.filter( ( slug ) => !( slug === "us" ? this.data.us : this.data.areas.find( ( a ) => a.slug === slug ) ).downloaded );
    const total = slugs.reduce( ( sum , slug ) => sum + this.sizeOf( slug ) , 0 );
    const fetchBytes = toFetch.reduce( ( sum , slug ) => sum + this.sizeOf( slug ) , 0 );
    Dom.text( Dom.get( "mapdata-summary" ) , slugs.length === 0 ? I18n.get( "mapdata.summary_none" ) : Format.fill( "mapdata.summary" , {
      count: slugs.length, size: this.bytes( total ), fetch: this.bytes( fetchBytes ),
    } ) );
    // Building holds about 3.5 times the extract in memory (Ohio: 330 MB -> ~1.1 GB),
    // beside the map the server already holds (~1 KB per road segment).
    const need = total * 3.5 + ( this.data.loaded.state === "ready" ? this.data.loaded.segments * 1000 : 0 );
    const warn = Dom.get( "mapdata-warning" );
    let text = "";
    if ( this.data.memory_bytes && need > this.data.memory_bytes * 0.7 ) {
      text = Format.fill( "mapdata.warn_memory" , { need: this.bytes( need ) , have: this.bytes( this.data.memory_bytes ) } );
    } else if ( this.data.disk_free_bytes && fetchBytes + total * 0.3 > this.data.disk_free_bytes ) {
      text = Format.fill( "mapdata.warn_disk" , { free: this.bytes( this.data.disk_free_bytes ) } );
    }
    Dom.text( warn , text );
    Dom.show( warn , text !== "" );
    const noBuild = Dom.get( "mapdata-no-build" );
    Dom.text( noBuild , I18n.get( "mapdata.no_osmium" ) );
    Dom.show( noBuild , !this.data.can_build );
    Dom.get( "mapdata-download" ).disabled = busy || slugs.length === 0;
    Dom.get( "mapdata-build" ).disabled = busy || slugs.length === 0 || !this.data.can_build;
  },

  // --- actions ----------------------------------------------------------------

  async run( body ) {
    try {
      this.data = await Api.post( "/api/owner/mapdata/run" , body );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    this.render();
    Dom.get( "mapdata-task-card" ).scrollIntoView( { behavior: "smooth" , block: "nearest" } );
  },

  async download() {
    const slugs = this.pickedSlugs();
    const missing = slugs.filter( ( slug ) => !( slug === "us" ? this.data.us : this.data.areas.find( ( a ) => a.slug === slug ) ).downloaded );
    if ( missing.length === 0 ) {
      if ( !( await Dom.confirm( I18n.get( "mapdata.confirm_fresh" ) ) ) ) return;
      return this.run( { areas: slugs , download: "all" , build: false , activate: false } );
    }
    return this.run( { areas: slugs , download: "missing" , build: false , activate: false } );
  },

  async build() {
    const slugs = this.pickedSlugs();
    if ( slugs.includes( "us" ) && !( await Dom.confirm( I18n.get( "mapdata.confirm_us" ) ) ) ) return;
    return this.run( { areas: slugs , download: "missing" , build: true , activate: Dom.get( "mapdata-activate" ).checked } );
  },

  async cancel() {
    if ( !( await Dom.confirm( I18n.get( "mapdata.confirm_cancel" ) ) ) ) return;
    try {
      this.data = await Api.post( "/api/owner/mapdata/cancel" );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    this.render();
  },

  async activate( graph ) {
    try {
      this.data = await Api.post( "/api/owner/mapdata/activate" , { region: graph.region } );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    Dom.toast( Format.fill( "mapdata.switching_to" , { map: this.areasLabel( graph.areas ) } ) , "ok" );
    this.render();
  },

  async remove( kind , name , label ) {
    if ( !( await Dom.confirm( Format.fill( kind === "map" ? "mapdata.confirm_delete_map" : "mapdata.confirm_delete_extract" , { name: label } ) ) ) ) return;
    try {
      this.data = await Api.post( "/api/owner/mapdata/delete" , { kind: kind , name: name } );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    Dom.toast( Format.fill( "mapdata.deleted" , { name: label } ) , "ok" );
    this.render();
  },

  // --- built maps and downloaded extracts --------------------------------------

  actionButton( icon , key , kind , onClick ) {
    return Dom.el( "button" , {
      class: "btn btn-sm btn-outline-" + kind,
      attrs: { type: "button" },
      children: [ Dom.el( "i" , { class: "bi " + icon , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { text: " " + I18n.get( key ) } ) ],
      on: { click: onClick },
    } );
  },

  renderGraphs() {
    const table = Dom.get( "mapdata-graphs" );
    const busy = this.data.task && this.data.task.state === "running";
    const rows = this.data.graphs.map( ( graph ) => {
      const label = this.areasLabel( graph.areas );
      const actions = [];
      if ( graph.active ) actions.push( this.badge( "success" , I18n.get( "mapdata.in_use" ) ) );
      else {
        const use = this.actionButton( "bi-check2-circle" , "mapdata.use" , "primary" , () => this.activate( graph ) );
        use.disabled = this.data.loaded.switching;
        actions.push( use );
        const del = this.actionButton( "bi-trash" , "mapdata.delete" , "danger" , () => this.remove( "map" , graph.region , label ) );
        del.disabled = busy;
        actions.push( del );
      }
      return Dom.el( "tr" , { children: [
        Dom.el( "td" , { text: label } ),
        Dom.el( "td" , { class: "text-nowrap" , text: this.bytes( graph.file.bytes ) } ),
        Dom.el( "td" , { class: "text-nowrap" , text: this.when( graph.file.modified ) } ),
        Dom.el( "td" , { children: [ Dom.el( "div" , { class: "d-flex flex-wrap gap-1" , children: actions } ) ] } ),
      ] } );
    } );
    Dom.swap( table.querySelector( "tbody" ) , ...rows );
    if ( rows.length === 0 ) Pager.empty( table );
  },

  renderExtracts() {
    Dom.text( Dom.get( "mapdata-folder" ) , this.data.folder );
    Dom.text( Dom.get( "mapdata-disk" ) , this.data.disk_free_bytes ? Format.fill( "mapdata.disk_free" , { free: this.bytes( this.data.disk_free_bytes ) } ) : "" );
    const table = Dom.get( "mapdata-extracts" );
    const busy = this.data.task && this.data.task.state === "running";
    const all = [ this.data.us , ...this.data.areas ].filter( ( a ) => a.downloaded );
    const rows = all.map( ( area ) => {
      const update = this.actionButton( "bi-arrow-repeat" , "mapdata.update" , "secondary" ,
        () => this.run( { areas: [ area.slug ] , download: "all" , build: false , activate: false } ) );
      const del = this.actionButton( "bi-trash" , "mapdata.delete" , "danger" , () => this.remove( "extract" , area.slug , area.name ) );
      update.disabled = busy;
      del.disabled = busy;
      return Dom.el( "tr" , { children: [
        Dom.el( "td" , { text: area.name } ),
        Dom.el( "td" , { class: "text-nowrap" , text: this.bytes( area.downloaded.bytes ) } ),
        Dom.el( "td" , { class: "text-nowrap" , text: this.when( area.downloaded.modified ) } ),
        Dom.el( "td" , { children: [ Dom.el( "div" , { class: "d-flex flex-wrap gap-1" , children: [ update , del ] } ) ] } ),
      ] } );
    } );
    Dom.swap( table.querySelector( "tbody" ) , ...rows );
    if ( rows.length === 0 ) Pager.empty( table );
  },
};
