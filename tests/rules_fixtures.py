"""Real rules texts copied from market snapshots on 2026-11-06, keyed by market id."""

RULES = {
    'KXBTCD-26NOV0600-T75299.99': (
        "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time I"
        'ndex (BRTI) before 12 AM EST is above 75299.99 at 12 AM EST on Nov 6, 2026, then'
        ' the market resolves to Yes.\n\nNot all cryptocurrency price data is the same. Whi'
        'le checking a source like Google or Coinbase may help guide your decision, the p'
        "rice used to determine this market is based on CF Benchmarks' corresponding Real"
        ' Time Index (RTI). At the last minute before expiration, 60 RTI prices are colle'
        'cted. The official and final value is the average of these prices.'
    ),
    'cpc-btc-above-hr-2026-11-06-0500z-75300': (
        'This market will settle to Yes if the price of Bitcoin is at or above $75,300 at'
        ' 12:00AM ET on Nov 6, 2026. Otherwise, this market will settle to No. Outcome so'
        "urced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at https://w"
        'ww.cfbenchmarks.com/data/indices/BRTI. The settlement value is determined as fol'
        'lows: in the last minute before 12:00AM ET on Nov 6, 2026, 60 BRTI prices are co'
        'llected. The official and final value is the simple average of these prices, rou'
        'nded to the nearest 2 decimal places. The market settles to Yes if the settlemen'
        't value is greater than or equal to $75,300. A settlement value exactly equal to'
        ' $75,300 settles to Yes. Please note that this market is about the price accordi'
        "ng to CF Benchmarks' BRTI, not according to other sources or spot markets."
    ),
    'KXBTCD-26NOV0523-T75899.99': (
        "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time I"
        'ndex (BRTI) before 11 PM EST is above 75899.99 at 11 PM EST on Nov 5, 2026, then'
        ' the market resolves to Yes.\n\nNot all cryptocurrency price data is the same. Whi'
        'le checking a source like Google or Coinbase may help guide your decision, the p'
        "rice used to determine this market is based on CF Benchmarks' corresponding Real"
        ' Time Index (RTI). At the last minute before expiration, 60 RTI prices are colle'
        'cted. The official and final value is the average of these prices.'
    ),
    'cpc-btc-above-hr-2026-11-06-0400z-75900': (
        'This market will settle to Yes if the price of Bitcoin is at or above $75,900 at'
        ' 11:00PM ET on Nov 5, 2026. Otherwise, this market will settle to No. Outcome so'
        "urced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at https://w"
        'ww.cfbenchmarks.com/data/indices/BRTI. The settlement value is determined as fol'
        'lows: in the last minute before 11:00PM ET on Nov 5, 2026, 60 BRTI prices are co'
        'llected. The official and final value is the simple average of these prices, rou'
        'nded to the nearest 2 decimal places. The market settles to Yes if the settlemen'
        't value is greater than or equal to $75,900. A settlement value exactly equal to'
        ' $75,900 settles to Yes. Please note that this market is about the price accordi'
        "ng to CF Benchmarks' BRTI, not according to other sources or spot markets."
    ),
    'KXETHD-26NOV0521-T2099.99': (
        "If the simple average of the sixty seconds of CF Benchmarks' Ethereum Real-Time "
        'Index (ERTI) before 9 PM EST is above 2099.99 at 9 PM EST on Nov 5, 2026, then t'
        'he market resolves to Yes.\n\nNot all cryptocurrency price data is the same. While'
        ' checking a source like Google or Coinbase may help guide your decision, the pri'
        "ce used to determine this market is based on CF Benchmarks' corresponding Real T"
        'ime Index (RTI). At the last minute before expiration, 60 RTI prices are collect'
        'ed. The official and final value is the average of these prices.'
    ),
    'cpceth-eth-above-hr-2026-11-06-0200z-2100': (
        'This market will settle to Yes if the price of Ethereum is at or above $2,100 at'
        ' 9:00PM ET on Nov 5, 2026. Otherwise, this market will settle to No. Outcome sou'
        "rced from CF Benchmarks' Ether-Dollar Real-Time Index (ETHUSD_RTI), available at"
        ' https://www.cfbenchmarks.com/data/indices/ETHUSD_RTI. The settlement value is d'
        'etermined as follows: in the last minute before 9:00PM ET on Nov 5, 2026, 60 ETH'
        'USD_RTI prices are collected. The official and final value is the simple average'
        ' of these prices, rounded to the nearest 2 decimal places. The market settles to'
        ' Yes if the settlement value is greater than or equal to $2,100. A settlement va'
        'lue exactly equal to $2,100 settles to Yes. Please note that this market is abou'
        "t the price according to CF Benchmarks' ETHUSD_RTI, not according to other sourc"
        'es or spot markets.'
    ),
    'KXBTC-26NOV0617-B67250': (
        "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time I"
        'ndex (BRTI) before 5 PM EST is between 67000-67499.99 at 5 PM EST on Nov 6, 2026'
        ', then the market resolves to Yes.\n\nNot all cryptocurrency price data is the sam'
        'e. While checking a source like Google or Coinbase may help guide your decision,'
        " the price used to determine this market is based on CF Benchmarks' correspondin"
        'g Real Time Index (RTI). At the last minute before expiration, 60 RTI prices are'
        ' collected. The official and final value is the average of these prices.'
    ),
    'cpc-btc-range-wk-2026-11-06-67000': (
        'This market will settle to Yes if the price of Bitcoin is between $67,000 and $6'
        '7,499.99, inclusive of both bounds at 5:00PM ET on Nov 6, 2026. Otherwise, this '
        'market will settle to No. Only one outcome in this event will settle to Yes. Out'
        "come sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at ht"
        'tps://www.cfbenchmarks.com/data/indices/BRTI. The settlement value is determined'
        ' as follows: in the last minute before 5:00PM ET on Nov 6, 2026, 60 BRTI prices '
        'are collected. The official and final value is the simple average of these price'
        's, rounded to the nearest 2 decimal places. Please note that this market is abou'
        "t the price according to CF Benchmarks' BRTI, not according to other sources or "
        'spot markets.'
    ),
    'KXBTC15M-26NOV052330-30': (
        "If the simple average of the sixty seconds of CF Benchmarks' BRTI before 11:30 P"
        'M EST on Nov 5, 2026 is at least the simple average of the sixty seconds of CF B'
        "enchmarks' BRTI before 11:15 PM EST on November 5, 2026, then the market resolve"
        's to Yes.\n\nNot all cryptocurrency price data is the same. While checking a sourc'
        'e like Google or Coinbase may help guide your decision, the price used to determ'
        "ine this market is based on CF Benchmarks' corresponding Real Time Index (RTI). "
        'At the last minute before expiration, 60 RTI prices are collected. The official '
        'and final value is the average of these prices, rounded to the nearest 2 decimal'
        ' places.'
    ),
    'cpc-btc-updown-15m-2026-11-06-0415z': (
        'This market will settle to Up if the Bitcoin price at 11:30PM ET on Nov 5, 2026 '
        'is greater than or equal to the price at 11:15PM ET on Nov 5, 2026. Otherwise, t'
        "his market will settle to Down. Outcome sourced from CF Benchmarks' Bitcoin Real"
        '-Time Index (BRTI), available at https://www.cfbenchmarks.com/data/indices/BRTI.'
        ' The price at the beginning and at the end of the time range is determined the s'
        'ame way: in the last minute before each of those two times, 60 BRTI prices are c'
        'ollected. The official value for that time is the simple average of these prices'
        ', rounded to the nearest 2 decimal places. Please note that this market is about'
        " the price according to CF Benchmarks' BRTI, not according to other sources or s"
        'pot markets.'
    ),
    'KXBTCMAXY-26DEC31-149999.99': (
        'If the Bitcoin spot price according to the CF Bitcoin Real-Time Index is above $'
        '149999.99 starting 01/01/2026 12:00 AM and before Dec 31, 2026 at 11:59 PM ET, t'
        'hen the market resolves to Yes.\n\nThe market resolves based on the CF Bitcoin Rea'
        'l-Time Index (BRTI) using a trimmed mean calculation. The resolution value is ca'
        'lculated by taking all BRTI values for each minute from market issuance until th'
        'e specified time on the target date, removing the top 20% and bottom 20% of valu'
        'es, then averaging the remaining 60% of values. If the BRTI crosses the threshol'
        'd at any point during the measurement period (triggering early expiration), the '
        'market immediately resolves. If no data is available at the expiration time, the'
        ' market resolves to No. The market can resolve early if Bitcoin definitively cro'
        'sses the threshold before the target date and time.'
    ),
    'cpc-btc-hitprice-high-yr-12-31-2026-150k': (
        'This market will settle to Yes if the price of Bitcoin is above $149,999.99 at a'
        'ny point from the creation of this market until 12:00 AM ET on January 1, 2027.\n'
        "\nOutcome sourced from CF Benchmarks' Bitcoin Real-Time Index.\n\nThe market settle"
        's based on the CF Bitcoin Real-Time Index (BRTI) using a trimmed mean calculatio'
        'n over any sixty second period, excluding the top 20% and bottom 20% of values. '
        'If this trimmed mean crosses the threshold at any point, the market settles to Y'
        'es. Otherwise, the market settles to No.'
    ),
}
