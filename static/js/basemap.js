// The picture under every map (the planner's, Navigate's): one of two.
//
// - "local": OpenStreetMap's own raster tiles, names in the local script only.
// - "english": OpenFreeMap's vector tiles drawn in the browser by MapLibre
//   (static/vendor/maplibre/), whose "liberty" style puts each name's Latin
//   form (the English name where OpenStreetMap has one, else a
//   transliteration) above the local one, so Arabic, Hebrew or Cyrillic
//   places can still be read.
//
// The vector map is not a Leaflet pane: Navigate turns its map with
// leaflet-rotate, which rotates the panes by CSS, and a canvas the size of
// the map turned that way leaves corners bare. So MapLibre draws on its own
// unturned canvas under all of Leaflet's panes, told the same center, zoom
// and bearing whenever the Leaflet map moves (VectorLayer below).
//
// The choice is the user's (PlannerPrefs.map_labels); planner.js calls
// Basemap.set once it has the prefs and again when the switch is flipped.
// MapLibre is about 1.4 MB, so it is loaded only the first time it is needed.
const Basemap = {
  labels: "local",
  maps: [],
  loading: null,

  // Put the current picture under map; it follows later Basemap.set calls.
  add( map ) {
    const entry = { map: map , layer: null , labels: null };
    this.maps.push( entry );
    this.apply( entry );
  },

  set( labels ) {
    const want = labels === "english" ? "english" : "local";
    if ( want === this.labels ) return;
    this.labels = want;
    this.maps.forEach( ( entry ) => this.apply( entry ) );
  },

  async apply( entry ) {
    const want = this.labels;
    if ( entry.labels === want ) return;
    let layer;
    if ( want === "english" ) {
      try {
        await this.loadMapLibre();
      } catch ( error ) {
        App.showError( I18n.get( "plan.map_labels_failed" ) );
        return;
      }
      if ( this.labels !== want ) return;
      layer = new this.VectorLayer( {
        style: "https://tiles.openfreemap.org/styles/liberty",
        attribution: I18n.get( "plan.map_attribution_english" ),
      } );
    } else {
      layer = L.tileLayer( "https://tile.openstreetmap.org/{z}/{x}/{y}.png" , {
        maxZoom: 19,
        // The app's own pages send no Referer off-site (Referrer-Policy:
        // same-origin), but OpenStreetMap's tile servers block requests that
        // carry none. The tiles alone send the site's origin, never a path.
        referrerPolicy: "strict-origin-when-cross-origin",
        attribution: I18n.get( "plan.map_attribution" ),
      } );
    }
    if ( entry.layer ) entry.map.removeLayer( entry.layer );
    entry.layer = layer;
    entry.labels = want;
    layer.addTo( entry.map );
    // Below the routes and pins, which live in their own layer groups.
    if ( layer.bringToBack ) layer.bringToBack();
  },

  loadMapLibre() {
    if ( !this.loading ) {
      const script = ( src ) => new Promise( ( resolve , reject ) => {
        const tag = document.createElement( "script" );
        tag.src = src;
        tag.onload = resolve;
        tag.onerror = reject;
        document.head.appendChild( tag );
      } );
      const css = document.createElement( "link" );
      css.rel = "stylesheet";
      css.href = "/vendor/maplibre/maplibre-gl.css";
      document.head.appendChild( css );
      this.loading = script( "/vendor/maplibre/maplibre-gl-csp.js" )
        .then( () => {
          // The CSP build: the worker is a file of ours, not a blob.
          maplibregl.setWorkerUrl( "/vendor/maplibre/maplibre-gl-csp-worker.js" );
          this.VectorLayer = this.vectorLayerClass();
        } )
        .catch( ( error ) => { this.loading = null; throw error; } );
    }
    return this.loading;
  },

  // A Leaflet layer that is a MapLibre map behind the Leaflet one.
  vectorLayerClass() {
    return L.Layer.extend( {
      initialize( options ) {
        L.setOptions( this , options );
      },

      getAttribution() {
        return this.options.attribution;
      },

      onAdd( map ) {
        const box = L.DomUtil.create( "div" , "basemap-vector" );
        // Under Leaflet's panes (z-index 400 up), which draw the routes and pins.
        Object.assign( box.style , { position: "absolute" , inset: "0" , zIndex: "0" , pointerEvents: "none" } );
        map.getContainer().insertBefore( box , map.getContainer().firstChild );
        this.box = box;
        this.gl = new maplibregl.Map( Object.assign( { container: box , style: this.options.style ,
          interactive: false , attributionControl: false , fadeDuration: 0 } , this.view() ) );
        map.on( "move zoom rotate viewreset" , this.sync , this );
        map.on( "zoomanim" , this.zoomTo , this );
        map.on( "resize" , this.resize , this );
      },

      onRemove( map ) {
        map.off( "move zoom rotate viewreset" , this.sync , this );
        map.off( "zoomanim" , this.zoomTo , this );
        map.off( "resize" , this.resize , this );
        this.gl.remove();
        this.box.remove();
      },

      // MapLibre's tiles are 512px to Leaflet's 256: one zoom level apart.
      // leaflet-rotate turns the map clockwise by its bearing; MapLibre's
      // bearing is the compass direction at the top, the other way round.
      view( center , zoom ) {
        const map = this._map;
        const at = center || map.getCenter();
        const bearing = typeof map.getBearing === "function" ? map.getBearing() : 0;
        return { center: [ at.lng , at.lat ] , zoom: ( zoom === undefined ? map.getZoom() : zoom ) - 1 , bearing: -bearing };
      },

      sync() {
        this.gl.jumpTo( this.view() );
      },

      // A zoom animation: go straight to where it ends.
      zoomTo( event ) {
        this.gl.jumpTo( this.view( event.center , event.zoom ) );
      },

      resize() {
        this.gl.resize();
        this.sync();
      },
    } );
  },
};
