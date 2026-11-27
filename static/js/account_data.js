// The "Your data" card on the Settings screen: download everything this
// person keeps here as one JSON file, or load such a file back (merged; see
// app/routes/account_data.py).
const AccountData = {
  init() {
    Dom.get( "data-export" ).addEventListener( "click" , () => this.download() );
    const input = Dom.get( "data-import-file" );
    Dom.get( "data-import" ).addEventListener( "click" , () => input.click() );
    input.addEventListener( "change" , () => {
      const file = input.files && input.files[ 0 ];
      input.value = "";
      if ( file ) this.load( file );
    } );
  },

  async download() {
    try {
      const data = await Api.request( "/api/account/export" );
      const day = new Date().toISOString().slice( 0 , 10 );
      Dom.download( "backroad-route-finder-" + day + ".json" , JSON.stringify( data , null , 1 ) + "\n" , "application/json" );
    } catch ( error ) {
      Dom.toast( error.message , "error" );
    }
  },

  async load( file ) {
    let data;
    try {
      data = JSON.parse( await file.text() );
    } catch ( _error ) {
      Dom.toast( I18n.get( "data.not_json" ) , "error" );
      return;
    }
    try {
      const reply = await Api.post( "/api/account/import" , { data: data } );
      Dom.toast( Format.fill( "data.imported" , { added: reply.routes_added , skipped: reply.routes_skipped , themes: reply.themes_added , keys: reply.api_keys_restored + reply.shared_keys_restored } ) , "ok" );
      SavedRoutes.refresh();
      if ( reply.api_keys_restored + reply.shared_keys_restored > 0 && typeof ProviderKeys !== "undefined" && ProviderKeys.me ) {
        ProviderKeys.load();
        if ( ProviderKeys.me.is_owner ) ProviderKeys.loadShared();
      }
    } catch ( error ) {
      Dom.toast( error.message , "error" );
    }
  },
};
