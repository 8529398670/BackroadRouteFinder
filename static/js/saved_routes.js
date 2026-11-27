// The Saved routes screen: your routes, to open in the planner, rename,
// share or delete, and the routes other people shared with you by name.
// Deleting is soft on the server, so it asks nothing and offers Undo, which
// restores it. A route's link is /r/<token> (planner.js opens it).
const SavedRoutes = {
  async init() {
    window.addEventListener( "nav" , ( event ) => {
      if ( event.detail.screen === "routes" ) this.refresh();
    } );
    Dom.get( "routes-export" ).addEventListener( "click" , () => this.exportZip() );
    const input = Dom.get( "routes-import-file" );
    Dom.get( "routes-import" ).addEventListener( "click" , () => input.click() );
    input.addEventListener( "change" , () => {
      const file = input.files && input.files[ 0 ];
      input.value = "";
      if ( file ) this.importFile( file );
    } );
    await this.refresh();
  },

  // --- export and import as a zip ---------------------------------------------
  //
  // The zip: manifest.json (an account file holding only the saved routes,
  // so loading it goes through POST /api/account/import, which skips routes
  // already here), routes/<name>.json one per route, and with GPX ticked
  // gpx/<name>.gpx, each route planned again for its line.

  async exportZip() {
    let withGpx = false;
    const answer = await Dom.form( {
      title: I18n.get( "routes.export_title" ),
      save: ( values ) => { withGpx = Boolean( values.gpx ); },
      submit: I18n.get( "routes.export" ),
      fields: [ { name: "gpx" , type: "checkbox" , label: I18n.get( "routes.export_gpx" ) , value: false , help: I18n.get( "routes.export_gpx_help" ) } ],
    } );
    if ( answer === null ) return;
    let data;
    try {
      data = await Api.request( "/api/account/export" );
    } catch ( error ) {
      Dom.toast( error.message , "error" );
      return;
    }
    const routes = data.saved_routes || [];
    if ( !routes.length ) {
      Dom.toast( I18n.get( "routes.export_none" ) , "error" );
      return;
    }
    const used = new Set();
    const unique = ( name ) => {
      const base = Export.fileName( name );
      let pick = base;
      for ( let n = 2 ; used.has( pick.toLowerCase() ) ; n++ ) pick = base + "-" + n;
      used.add( pick.toLowerCase() );
      return pick;
    };
    const day = new Date().toISOString().slice( 0 , 10 );
    const files = [ { path: "manifest.json" , data: JSON.stringify( { format: data.format , exported_at: data.exported_at , saved_routes: routes } , null , 1 ) + "\n" } ];
    const names = routes.map( ( route ) => unique( route.name ) );
    routes.forEach( ( route , index ) => files.push( { path: "routes/" + names[ index ] + ".json" , data: JSON.stringify( route , null , 1 ) + "\n" } ) );
    const failed = [];
    if ( withGpx ) {
      for ( let index = 0 ; index < routes.length ; index++ ) {
        const route = routes[ index ];
        Dom.toast( Format.fill( "routes.exporting" , { n: index + 1 , total: routes.length } ) , "ok" );
        try {
          const result = await Api.post( "/api/route" , Planner.bodyFor( route ) );
          const choices = result.routes.filter( ( choice ) => !choice.compare );
          const pick = route.options.pick;
          const chosen = ( pick && choices.find( ( choice ) => choice.source === pick.source && ( choice.rank || 0 ) === pick.rank ) ) || choices[ 0 ];
          if ( !chosen ) throw new Error( "no route" );
          files.push( { path: "gpx/" + names[ index ] + ".gpx" , data: Export.gpx( route.name , route.waypoints , chosen ) } );
        } catch ( error ) {
          failed.push( route.name );
        }
      }
    }
    Dom.download( "backroad-routes-" + day + ".zip" , Zip.build( files ) , "application/zip" );
    if ( failed.length ) Dom.toast( Format.fill( "routes.export_gpx_failed" , { names: failed.join( ", " ) } ) , "error" );
    else Dom.toast( Format.fill( "routes.exported" , { n: routes.length } ) , "ok" );
  },

  // A zip from exportZip (or an account .json file): its routes merged in.
  async importFile( file ) {
    let data;
    try {
      if ( /\.json$/i.test( file.name ) ) {
        data = JSON.parse( await file.text() );
      } else {
        const entries = await Zip.read( file );
        const text = ( bytes ) => new TextDecoder().decode( bytes );
        const manifest = Object.keys( entries ).find( ( path ) => /(^|\/)manifest\.json$/i.test( path ) );
        if ( manifest ) data = JSON.parse( text( entries[ manifest ] ) );
        else {
          const routes = Object.keys( entries ).filter( ( path ) => /(^|\/)routes\/[^/]+\.json$/i.test( path ) ).sort()
            .map( ( path ) => JSON.parse( text( entries[ path ] ) ) );
          data = { format: "backroad-route-finder/1" , saved_routes: routes };
        }
      }
    } catch ( _error ) {
      Dom.toast( I18n.get( "routes.import_bad" ) , "error" );
      return;
    }
    if ( !data || !Array.isArray( data.saved_routes ) ) {
      Dom.toast( I18n.get( "routes.import_bad" ) , "error" );
      return;
    }
    try {
      // Only the routes: a whole account file loads from Settings.
      const reply = await Api.post( "/api/account/import" , { data: { format: data.format , saved_routes: data.saved_routes } } );
      Dom.toast( Format.fill( "routes.imported" , { added: reply.routes_added , skipped: reply.routes_skipped } ) , "ok" );
    } catch ( error ) {
      Dom.toast( error.message , "error" );
    }
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
