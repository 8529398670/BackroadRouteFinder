// The Saved routes screen: your routes, to open in the planner, rename or
// delete. Deleting is soft on the server, so it asks nothing and offers Undo,
// which restores it.
const SavedRoutes = {
  async init() {
    window.addEventListener( "nav" , ( event ) => {
      if ( event.detail.screen === "routes" ) this.refresh();
    } );
    await this.refresh();
  },

  fetchPage( offset ) {
    return Api.request( "/api/saved-routes?limit=200&offset=" + ( offset || 0 ) );
  },

  async refresh() {
    await Pager.load( "routes-table" , "routes" , ( offset ) => this.fetchPage( offset ) , ( route ) => this.row( route ) );
  },

  stopsText( route ) {
    const short = ( point ) => ( point.label || "" ).split( "," )[ 0 ];
    const first = route.waypoints[ 0 ];
    const last = route.waypoints[ route.waypoints.length - 1 ];
    const more = route.waypoints.length - 2;
    return more > 0
      ? Format.fill( "routes.stops_more" , { from: short( first ) , to: short( last ) , n: more } )
      : Format.fill( "routes.stops" , { from: short( first ) , to: short( last ) } );
  },

  row( route ) {
    const tbody = Dom.get( "routes-table" ).querySelector( "tbody" );
    let name = route.name;
    const nameCell = Dom.editable( route.name , {
      label: I18n.get( "routes.name_label" ),
      maxLength: 80,
      save: async ( value ) => {
        const previous = name;
        await Api.post( "/api/saved-routes/" + route.id + "/rename" , { name: value , expect: previous } );
        name = value;
        Undo.record( {
          label: I18n.get( "undo.route_rename" ),
          toast: true,
          undo: async () => { await Api.post( "/api/saved-routes/" + route.id + "/rename" , { name: previous , expect: value } ); await this.refresh(); },
          redo: async () => { await Api.post( "/api/saved-routes/" + route.id + "/rename" , { name: value , expect: previous } ); await this.refresh(); },
        } );
      },
    } );
    const summary = route.summary || {};
    const actions = Dom.rowActions( route.name , [
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-primary",
        attrs: { type: "button" },
        children: [ Dom.el( "i" , { class: "bi bi-map" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.open" ) } ) ],
        on: { click: () => Planner.open( route ) },
      } ),
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-danger",
        attrs: { type: "button" , "data-write": "" },
        children: [ Dom.el( "i" , { class: "bi bi-trash" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.delete" ) } ) ],
        on: { click: () => this.remove( route ) },
      } ),
    ] );
    tbody.appendChild( Dom.el( "tr" , { children: [
      Dom.el( "td" , { children: [ nameCell ] } ),
      Dom.el( "td" , { text: this.stopsText( route ) } ),
      Dom.el( "td" , { class: "text-nowrap" , text: Format.duration( summary.seconds || 0 ) + " · " + Format.distance( summary.meters || 0 ) } ),
      Dom.el( "td" , { class: "text-nowrap" , text: Format.share( summary.main_seconds || 0 , summary.seconds || 0 ) } ),
      Dom.el( "td" , { children: [ Dom.when( route.updated_at ) ] } ),
      Dom.el( "td" , { class: "text-end" , children: [ actions ] } ),
    ] } ) );
  },

  async remove( route ) {
    await Api.post( "/api/saved-routes/" + route.id + "/delete" );
    await this.refresh();
    Undo.record( {
      label: I18n.get( "undo.route_delete" ),
      toast: true,
      undo: async () => { await Api.post( "/api/saved-routes/" + route.id + "/restore" ); await this.refresh(); },
      redo: async () => { await Api.post( "/api/saved-routes/" + route.id + "/delete" ); await this.refresh(); },
    } );
  },
};
