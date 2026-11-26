// The Settings screen (/settings), for everyone.
//
// "Routing engines and API keys": one row per other routing engine, with this
// person's own key and a Test button. Their key always wins over a shared one.
//
// "Shared keys for everyone" (owner only): the owner's keys, and per engine
// whether people may use them ("shared") or must bring their own ("own").
//
// Keys go to the server and are sealed in the database. None is ever sent
// back: a row shows only that a key is set and its last four characters.
// Engines with no key (Valhalla, OSRM) use their address from the server
// settings' Providers section.
const ProviderKeys = {
  me: null,

  async init( me ) {
    this.me = me;
    if ( me.is_owner ) {
      Dom.show( Dom.get( "shared-keys-card" ) , true );
      Dom.get( "shared-all-shared" ).addEventListener( "click" , () => this.setAll( "shared" ) );
      Dom.get( "shared-all-own" ).addEventListener( "click" , () => this.setAll( "own" ) );
      await Promise.all( [ this.load() , this.loadShared() ] );
      return;
    }
    await this.load();
  },

  // --- this person's own keys ------------------------------------------------

  async load() {
    let reply;
    try {
      reply = await Api.request( "/api/providers" );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    this.render( reply.engines );
  },

  render( engines ) {
    Dom.swap( Dom.get( "engines-rows" ) , ...engines.map( ( engine ) => this.row( engine ) ) );
    // The planner's Sources card follows what is usable now.
    if ( typeof Planner !== "undefined" && Planner.refreshSources ) Planner.refreshSources();
  },

  badge( kind , key ) {
    return Dom.el( "span" , { class: "badge text-bg-" + kind , text: I18n.get( key ) } );
  },

  status( engine ) {
    if ( engine.url_setting && !engine.url ) return this.badge( "secondary" , "settings.engines_off" );
    if ( !engine.needs_key ) return engine.usable ? this.badge( "success" , "settings.engines_ready" ) : this.badge( "secondary" , "settings.engines_off" );
    if ( engine.using === "yours" ) return this.badge( "success" , "settings.engines_using_yours" );
    if ( engine.using === "shared" ) return this.badge( "info" , "settings.engines_using_shared" );
    if ( engine.usable ) return this.badge( "success" , "settings.engines_ready" );
    if ( engine.policy === "own" && engine.shared_key_set && !this.me.is_owner ) return this.badge( "warning" , "settings.engines_needs_yours" );
    return this.badge( "warning" , "settings.engines_no_key" );
  },

  // Under the key box: where the key this person's plans use comes from.
  keyNote( engine ) {
    if ( this.me.is_owner ) return "";
    if ( engine.policy === "own" ) return I18n.get( "settings.engines_policy_own" );
    if ( engine.shared_key_set ) return I18n.get( engine.key_set ? "settings.engines_shared_backup" : "settings.engines_shared_available" );
    return "";
  },

  testButton( onClick ) {
    return Dom.el( "button" , {
      class: "btn btn-sm btn-outline-secondary",
      attrs: { type: "button" },
      children: [ Dom.el( "i" , { class: "bi bi-play-circle" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { text: " " + I18n.get( "settings.engines_test" ) } ) ],
      on: { click: onClick },
    } );
  },

  about( engine ) {
    const parts = [ Dom.el( "div" , { class: "fw-semibold" , text: engine.name } ) ];
    if ( engine.backroads ) parts.push( Dom.el( "div" , { class: "small text-body-secondary" , text: engine.backroads } ) );
    if ( engine.free_tier ) parts.push( Dom.el( "div" , { class: "small text-body-secondary" , text: engine.free_tier } ) );
    parts.push( Dom.el( "a" , {
      class: "small",
      text: I18n.get( engine.needs_key ? "settings.engines_get_key" : "settings.engines_site" ),
      attrs: { href: engine.signup_url , target: "_blank" , rel: "noopener noreferrer" },
    } ) );
    return Dom.el( "td" , { children: parts } );
  },

  // A key box: a password field and Save, then "Key set (…abcd) · Remove".
  keyForm( name , keySet , hint , save , remove ) {
    const input = Dom.el( "input" , {
      class: "form-control form-control-sm",
      attrs: {
        type: "password",
        autocomplete: "off",
        spellcheck: "false",
        placeholder: I18n.get( "settings.engines_key_placeholder" ),
        "aria-label": name + " " + I18n.get( "settings.engines_key" ),
      },
    } );
    const button = Dom.el( "button" , { class: "btn btn-sm btn-primary" , attrs: { type: "submit" } , text: I18n.get( "settings.engines_save" ) } );
    const form = Dom.el( "form" , { class: "input-group input-group-sm" , attrs: { autocomplete: "off" , "data-write": "" } , children: [ input , button ] } );
    form.addEventListener( "submit" , ( event ) => {
      event.preventDefault();
      const key = input.value.trim();
      if ( key.length < 8 ) {
        App.showError( I18n.get( "settings.engines_key_placeholder" ) );
        return;
      }
      Dom.busy( button , () => save( key ).catch( ( error ) => App.showError( error.message ) ) );
    } );
    const parts = [ form ];
    if ( keySet ) {
      parts.push( Dom.el( "div" , { class: "d-flex align-items-center gap-2 mt-1" , children: [
        Dom.el( "span" , { class: "small text-body-secondary" , text: Format.fill( "settings.engines_key_set" , { hint: hint } ) } ),
        Dom.el( "button" , {
          class: "btn btn-sm btn-link text-danger p-0",
          attrs: { type: "button" , "data-write": "" },
          text: I18n.get( "settings.engines_remove" ),
          on: { click: remove },
        } ),
      ] } ) );
    }
    return parts;
  },

  row( engine ) {
    const result = Dom.el( "div" , { class: "small mt-1" , attrs: { role: "status" } } );
    const test = this.testButton( () => this.test( "/api/providers/" + engine.id + "/test" , engine , result ) );
    const status = Dom.el( "td" , { children: [ this.status( engine ) , Dom.el( "div" , { class: "mt-2" , children: [ test ] } ) ] } );

    let keyCell;
    if ( !engine.needs_key ) {
      keyCell = Dom.el( "td" , { children: [
        Dom.el( "div" , { class: "small text-body-secondary" , text: I18n.get( "settings.engines_free" ) } ),
        engine.url_setting && this.me.is_owner ? Dom.el( "code" , { class: "small" , text: engine.url_setting + ( engine.url ? ": " + engine.url : "" ) } ) : null,
        result,
      ].filter( Boolean ) } );
    } else {
      const parts = this.keyForm( engine.name , engine.key_set , engine.key_hint ,
        ( key ) => this.saveKey( engine , key ) , () => this.removeKey( engine ) );
      const note = this.keyNote( engine );
      if ( note ) parts.push( Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: note } ) );
      if ( engine.url_setting && this.me.is_owner ) parts.push( Dom.el( "code" , { class: "small d-block mt-1" , text: engine.url_setting + ": " + engine.url } ) );
      parts.push( result );
      keyCell = Dom.el( "td" , { class: "engines-key" , children: parts } );
    }
    return Dom.el( "tr" , { attrs: { "data-key": engine.id } , children: [ this.about( engine ) , status , keyCell ] } );
  },

  async saveKey( engine , key ) {
    const reply = await Api.post( "/api/providers/" + engine.id + "/key" , { key: key } );
    Dom.toast( Format.fill( "settings.engines_saved" , { engine: engine.name } ) , "ok" );
    this.render( reply.engines );
  },

  async removeKey( engine ) {
    if ( !( await Dom.confirm( Format.fill( "settings.engines_remove_confirm" , { engine: engine.name } ) ) ) ) return;
    const reply = await Api.post( "/api/providers/" + engine.id + "/key/delete" );
    Dom.toast( Format.fill( "settings.engines_removed" , { engine: engine.name } ) , "ok" );
    this.render( reply.engines );
  },

  async test( path , engine , box ) {
    Dom.text( box , I18n.get( "settings.engines_testing" ) );
    box.className = "small mt-1 text-body-secondary";
    const reply = await Api.post( path );
    if ( reply.ok ) {
      Dom.text( box , Format.fill( "settings.engines_test_ok" , {
        engine: engine.name, ms: reply.milliseconds, routes: reply.routes,
        time: Format.duration( reply.seconds ), distance: Format.distance( reply.meters ),
      } ) );
      box.className = "small mt-1 text-success";
    } else {
      Dom.text( box , Format.fill( "settings.engines_test_failed" , { engine: engine.name , message: reply.message } ) );
      box.className = "small mt-1 text-danger";
    }
  },

  // --- the owner's shared keys and who may use them --------------------------

  async loadShared() {
    const reply = await Api.request( "/api/owner/providers" );
    this.renderShared( reply.engines );
  },

  renderShared( engines ) {
    this.shared = engines;
    Dom.swap( Dom.get( "shared-rows" ) , ...engines.map( ( engine ) => this.sharedRow( engine ) ) );
  },

  // After a change to the shared keys or the policy, this person's own card
  // changes too (which key they use, what is usable).
  async afterShared( reply ) {
    this.renderShared( reply.engines );
    await this.load();
  },

  sharedRow( engine ) {
    const result = Dom.el( "div" , { class: "small mt-1" , attrs: { role: "status" } } );
    const parts = this.keyForm( engine.name , engine.key_set , engine.key_hint ,
      ( key ) => this.saveShared( engine , key ) , () => this.removeShared( engine ) );
    parts.push( Dom.el( "div" , { class: "mt-1" , children: [
      this.testButton( () => this.test( "/api/owner/providers/" + engine.id + "/test" , engine , result ) ),
    ] } ) );
    parts.push( result );

    const id = "policy-" + engine.id;
    const select = Dom.el( "select" , {
      class: "form-select form-select-sm",
      attrs: { id: id , "aria-label": engine.name + " " + I18n.get( "settings.shared_policy" ) },
      children: [ "shared" , "own" ].map( ( mode ) => {
        const option = Dom.el( "option" , { text: I18n.get( "settings.shared_policy_" + mode ) , attrs: { value: mode } } );
        if ( engine.policy === mode ) option.selected = true;
        return option;
      } ),
      on: { change: ( event ) => this.setPolicy( { [ engine.id ]: event.target.value } ) },
    } );
    const hint = Dom.el( "div" , { class: "small text-body-secondary mt-1" , text: I18n.get( "settings.shared_policy_" + engine.policy + "_hint" ) } );
    return Dom.el( "tr" , { attrs: { "data-key": engine.id } , children: [
      Dom.el( "td" , { children: [
        Dom.el( "div" , { class: "fw-semibold" , text: engine.name } ),
        Dom.el( "a" , { class: "small" , text: I18n.get( "settings.engines_get_key" ) , attrs: { href: engine.signup_url , target: "_blank" , rel: "noopener noreferrer" } } ),
      ] } ),
      Dom.el( "td" , { class: "engines-key" , children: parts } ),
      Dom.el( "td" , { children: [ select , hint ] } ),
    ] } );
  },

  async saveShared( engine , key ) {
    const reply = await Api.post( "/api/owner/providers/" + engine.id + "/key" , { key: key } );
    Dom.toast( Format.fill( "settings.shared_saved" , { engine: engine.name } ) , "ok" );
    await this.afterShared( reply );
  },

  async removeShared( engine ) {
    if ( !( await Dom.confirm( Format.fill( "settings.shared_remove_confirm" , { engine: engine.name } ) ) ) ) return;
    const reply = await Api.post( "/api/owner/providers/" + engine.id + "/key/delete" );
    Dom.toast( Format.fill( "settings.shared_removed" , { engine: engine.name } ) , "ok" );
    await this.afterShared( reply );
  },

  async setPolicy( engines ) {
    try {
      const reply = await Api.post( "/api/owner/providers/policy" , { engines: engines } );
      Dom.toast( I18n.get( "settings.shared_policy_saved" ) , "ok" );
      await this.afterShared( reply );
    } catch ( error ) {
      App.showError( error.message );
    }
  },

  setAll( mode ) {
    const engines = {};
    ( this.shared || [] ).forEach( ( engine ) => { engines[ engine.id ] = mode; } );
    return this.setPolicy( engines );
  },
};
