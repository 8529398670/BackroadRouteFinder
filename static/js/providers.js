// The Settings screen's "Routing engines and API keys" card (owner only):
// one row per other routing engine, with its key and a Test button.
//
// Keys go to POST /api/owner/providers/{engine}/key and are sealed in the
// database by the server. None is ever sent back: a row shows only that a key
// is set and its last four characters. Engines with no key (Valhalla, OSRM)
// use their address from the Providers section of the settings form below.
const ProviderKeys = {
  async init( me ) {
    if ( !me.is_owner ) return;
    await this.load();
  },

  async load() {
    let reply;
    try {
      reply = await Api.request( "/api/owner/providers" );
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

  status( engine ) {
    if ( engine.usable ) return Dom.el( "span" , { class: "badge text-bg-success" , text: I18n.get( "settings.engines_ready" ) } );
    if ( engine.url_setting && !engine.url ) return Dom.el( "span" , { class: "badge text-bg-secondary" , text: I18n.get( "settings.engines_off" ) } );
    return Dom.el( "span" , { class: "badge text-bg-warning" , text: I18n.get( "settings.engines_no_key" ) } );
  },

  row( engine ) {
    const result = Dom.el( "div" , { class: "small mt-1" , attrs: { role: "status" } } );
    const link = Dom.el( "a" , {
      class: "small",
      text: I18n.get( engine.needs_key ? "settings.engines_get_key" : "settings.engines_site" ),
      attrs: { href: engine.signup_url , target: "_blank" , rel: "noopener noreferrer" },
    } );
    const about = Dom.el( "td" , { children: [
      Dom.el( "div" , { class: "fw-semibold" , text: engine.name } ),
      Dom.el( "div" , { class: "small text-body-secondary" , text: engine.backroads } ),
      Dom.el( "div" , { class: "small text-body-secondary" , text: engine.free_tier } ),
      link,
    ] } );
    const test = Dom.el( "button" , {
      class: "btn btn-sm btn-outline-secondary",
      attrs: { type: "button" },
      children: [ Dom.el( "i" , { class: "bi bi-play-circle" , attrs: { "aria-hidden": "true" } } ) , Dom.el( "span" , { text: " " + I18n.get( "settings.engines_test" ) } ) ],
      on: { click: () => this.test( engine , result ) },
    } );
    const status = Dom.el( "td" , { children: [ this.status( engine ) , Dom.el( "div" , { class: "mt-2" , children: [ test ] } ) ] } );

    let keyCell;
    if ( !engine.needs_key ) {
      keyCell = Dom.el( "td" , { children: [
        Dom.el( "div" , { class: "small text-body-secondary" , text: I18n.get( "settings.engines_free" ) } ),
        engine.url_setting ? Dom.el( "code" , { class: "small" , text: engine.url_setting + ( engine.url ? ": " + engine.url : "" ) } ) : null,
        result,
      ].filter( Boolean ) } );
    } else {
      const input = Dom.el( "input" , {
        class: "form-control form-control-sm",
        attrs: {
          type: "password",
          autocomplete: "off",
          spellcheck: "false",
          placeholder: I18n.get( "settings.engines_key_placeholder" ),
          "aria-label": engine.name + " " + I18n.get( "settings.engines_key" ),
        },
      } );
      const save = Dom.el( "button" , {
        class: "btn btn-sm btn-primary",
        attrs: { type: "submit" },
        text: I18n.get( "settings.engines_save" ),
      } );
      const form = Dom.el( "form" , {
        class: "input-group input-group-sm",
        attrs: { autocomplete: "off" },
        children: [ input , save ],
      } );
      form.addEventListener( "submit" , ( event ) => {
        event.preventDefault();
        Dom.busy( save , () => this.saveKey( engine , input.value.trim() ).catch( ( error ) => App.showError( error.message ) ) );
      } );
      const parts = [ form ];
      if ( engine.key_set ) {
        parts.push( Dom.el( "div" , { class: "d-flex align-items-center gap-2 mt-1" , children: [
          Dom.el( "span" , { class: "small text-body-secondary" , text: Format.fill( "settings.engines_key_set" , { hint: engine.key_hint } ) } ),
          Dom.el( "button" , {
            class: "btn btn-sm btn-link text-danger p-0",
            attrs: { type: "button" },
            text: I18n.get( "settings.engines_remove" ),
            on: { click: () => this.removeKey( engine ) },
          } ),
        ] } ) );
      }
      if ( engine.url_setting ) parts.push( Dom.el( "code" , { class: "small d-block mt-1" , text: engine.url_setting + ": " + engine.url } ) );
      parts.push( result );
      keyCell = Dom.el( "td" , { class: "engines-key" , children: parts } );
    }
    return Dom.el( "tr" , { attrs: { "data-key": engine.id } , children: [ about , status , keyCell ] } );
  },

  async saveKey( engine , key ) {
    if ( key.length < 8 ) {
      App.showError( I18n.get( "settings.engines_key_placeholder" ) );
      return;
    }
    const reply = await Api.post( "/api/owner/providers/" + engine.id + "/key" , { key: key } );
    Dom.toast( Format.fill( "settings.engines_saved" , { engine: engine.name } ) , "ok" );
    this.render( reply.engines );
  },

  async removeKey( engine ) {
    if ( !( await Dom.confirm( Format.fill( "settings.engines_remove_confirm" , { engine: engine.name } ) ) ) ) return;
    const reply = await Api.post( "/api/owner/providers/" + engine.id + "/key/delete" );
    Dom.toast( Format.fill( "settings.engines_removed" , { engine: engine.name } ) , "ok" );
    this.render( reply.engines );
  },

  async test( engine , box ) {
    Dom.text( box , I18n.get( "settings.engines_testing" ) );
    box.className = "small mt-1 text-body-secondary";
    const reply = await Api.post( "/api/owner/providers/" + engine.id + "/test" );
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
};
