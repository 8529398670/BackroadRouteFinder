// Times, distances and shares as the planner and the saved-routes list show
// them. The words and units come from language.yaml (plan.*), so changing
// "mi" or "min" there changes them everywhere.
const Format = {
  fill( key , values ) {
    return Object.entries( values ).reduce(
      ( text , entry ) => text.split( "{" + entry[ 0 ] + "}" ).join( String( entry[ 1 ] ) ) ,
      I18n.get( key ) ,
    );
  },

  duration( seconds ) {
    const minutes = Math.max( 0 , Math.round( seconds / 60 ) );
    if ( minutes < 60 ) return this.fill( "plan.minutes" , { m: minutes } );
    return this.fill( "plan.hours_minutes" , { h: Math.floor( minutes / 60 ) , m: minutes % 60 } );
  },

  distance( meters ) {
    const miles = meters / 1609.344;
    if ( miles < 0.2 ) return this.fill( "plan.feet" , { n: Math.round( meters * 3.28084 / 10 ) * 10 } );
    return this.fill( "plan.miles" , { n: miles < 10 ? miles.toFixed( 1 ) : Math.round( miles ) } );
  },

  share( part , whole ) {
    const percent = whole > 0 ? ( 100 * part ) / whole : 0;
    return this.fill( "plan.percent" , { n: percent < 10 && percent > 0 ? percent.toFixed( 1 ) : Math.round( percent ) } );
  },

  coord( value ) {
    return Number( value ).toFixed( 5 );
  },
};
