// The Saved routes screen: your routes, to open in the planner, rename,
// share or delete, and the routes other people shared with you by name.
// Deleting is soft on the server, so it asks nothing and offers Undo, which
// restores it. A route's link is /r/<token> (planner.js opens it).
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
    await Promise.all( [
      Pager.load( "routes-table" , "routes" , ( offset ) => this.fetchPage( offset ) , ( route ) => this.row( route ) ),
      Pager.load( "shared-routes-table" , "routes" , ( offset ) => Api.request( "/api/shared-routes?limit=200&offset=" + ( offset || 0 ) ) ,
        ( shared ) => this.sharedRow( shared ) ),
    ] );
  },

  linkUrl( token ) {
    return window.location.origin + Planner.linkPath( token );
  },

  async copyLink( token ) {
    const url = this.linkUrl( token );
    try {
      await navigator.clipboard.writeText( url );
      Dom.toast( I18n.get( "routes.link_copied" ) , "ok" );
    } catch ( error ) {
      // No clipboard on plain http: the link itself, to copy by hand.
      Dom.toast( Format.fill( "routes.link_copy_failed" , { url: url } ) , "ok" );
    }
  },

  // Who may view a route: anyone signed in with the link, and/or people by
  // name. Saving copies the link. Answers the route as saved, or null.
  async share( route ) {
    const me = App.currentUser ? App.currentUser.id : 0;
    let team = [];
    for ( let offset = 0 ; offset !== null && offset !== undefined ; ) {
      const page = await Api.request( "/api/team?limit=1000&offset=" + offset );
      team = team.concat( page.members || [] );
      offset = page.next;
    }
    const people = team.filter( ( member ) => member.id !== me ).map( ( member ) => ( { value: member.id , label: member.display_name } ) );
    const current = route.share || { link: false , users: [] };
    const fields = [ {
      name: "link" , type: "checkbox" , label: I18n.get( "routes.share_link" ) , value: current.link,
      help: Format.fill( "routes.share_link_help" , { url: this.linkUrl( route.token ) } ),
    } ];
    if ( people.length ) fields.push( { name: "users" , type: "checks" , label: I18n.get( "routes.share_people" ) , options: people , value: current.users } );
    else fields[ 0 ].help += " " + I18n.get( "routes.share_nobody" );
    let saved = null;
    const answer = await Dom.form( {
      title: Format.fill( "routes.share_title" , { name: route.name || "" } ),
      submit: I18n.get( "routes.share_save" ),
      fields: fields,
      save: async ( values ) => {
        saved = await Api.post( "/api/saved-routes/" + route.id + "/share" , { link: values.link , users: values.users || [] } );
      },
    } );
    if ( answer === null || !saved ) return null;
    if ( saved.share.link || saved.share.users.length ) await this.copyLink( saved.token );
    else Dom.toast( I18n.get( "routes.shared_private" ) , "ok" );
    await this.refresh();
    return saved;
  },

  sharedRow( shared ) {
    const route = shared.route;
    const tbody = Dom.get( "shared-routes-table" ).querySelector( "tbody" );
    const summary = route.summary || {};
    const actions = Dom.rowActions( route.name , [
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-primary",
        attrs: { type: "button" },
        children: [ Dom.el( "i" , { class: "bi bi-map" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.open" ) } ) ],
        on: { click: () => Planner.open( route , shared ) },
      } ),
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-primary",
        attrs: { type: "button" },
        children: [ Dom.el( "i" , { class: "bi bi-sign-turn-right" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.navigate" ) } ) ],
        on: { click: () => Planner.open( route , shared , true ) },
      } ),
    ] );
    tbody.appendChild( Dom.el( "tr" , { children: [
      Dom.el( "td" , { text: route.name } ),
      Dom.el( "td" , { text: this.stopsText( route ) } ),
      Dom.el( "td" , { text: shared.owner_name } ),
      Dom.el( "td" , { class: "text-nowrap" , text: Format.duration( summary.seconds || 0 ) + " · " + Format.distance( summary.meters || 0 ) } ),
      Dom.el( "td" , { class: "text-end" , children: [ actions ] } ),
    ] } ) );
  },

  stopsText( route ) {
    const short = ( point ) => ( point.label || "" ).split( "," )[ 0 ];
    const loop = route.options && route.options.loop;
    if ( loop ) {
      const center = route.waypoints[ 0 ] ? short( route.waypoints[ 0 ] ) : loop.center_lat.toFixed( 3 ) + ", " + loop.center_lon.toFixed( 3 );
      const radius = Format.fill( "plan.loop_radius_text" , { n: loop.radius , unit: I18n.get( loop.unit === "min" ? "plan.unit_min" : "plan.unit_mi" ) } );
      return Format.fill( "routes.loop" , { center: center , radius: radius } );
    }
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
        class: "btn btn-sm btn-outline-primary",
        attrs: { type: "button" },
        children: [ Dom.el( "i" , { class: "bi bi-sign-turn-right" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.navigate" ) } ) ],
        on: { click: () => Planner.open( route , null , true ) },
      } ),
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-secondary",
        attrs: { type: "button" , "data-write": "" },
        children: [ Dom.el( "i" , { class: "bi bi-share" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.share" ) } ) ],
        on: { click: () => this.share( Object.assign( {} , route , { name: name } ) ) },
      } ),
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-secondary",
        attrs: { type: "button" },
        children: [ Dom.el( "i" , { class: "bi bi-link-45deg" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.copy_link" ) } ) ],
        on: { click: () => this.copyLink( route.token ) },
      } ),
      Dom.el( "button" , {
        class: "btn btn-sm btn-outline-danger",
        attrs: { type: "button" , "data-write": "" },
        children: [ Dom.el( "i" , { class: "bi bi-trash" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { class: "ms-1" , text: I18n.get( "routes.delete" ) } ) ],
        on: { click: () => this.remove( route ) },
      } ),
    ] );
    const share = route.share || { link: false , users: [] };
    const badges = [
      share.link ? Dom.badge( I18n.get( "routes.shared_badge_link" ) , "info" ) : null,
      share.users.length ? Dom.badge( Format.fill( "routes.shared_badge_people" , { n: share.users.length } ) , "info" ) : null,
    ].filter( Boolean );
    tbody.appendChild( Dom.el( "tr" , { children: [
      Dom.el( "td" , { children: [ nameCell , ...( badges.length ? [ Dom.el( "div" , { class: "d-flex gap-1 mt-1" , children: badges } ) ] : [] ) ] } ),
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
