# Report: uscite e profilo crypto (Step 4 e 5)

Studio riproducibile: `cd backend && python -m app.scripts.exit_study --cache <cartella> --out report.md`. Usa i prezzi orari
ufficiali di Alpaca (crypto, senza chiave) di 6 coppie su un anno, i backtest walk-forward della piattaforma (lo stesso
motore del trading, costi inclusi) e la stessa simulazione degli shadow exit. Nessun ordine reale.

## Sintesi

1. **I costi dominano.** Con barre orarie lo stop è di 1,5–2,5 ATR (circa l'1–2% del prezzo) e il costo di andata e ritorno
   ipotizzato (commissione taker Alpaca 0,25% per lato + metà spread 5 bps + slippage 5 bps per lato) è circa lo 0,7% del
   prezzo: **0,45 R per operazione**. Con questi costi nessuna strategia e nessuna regola d'uscita è in attivo.
2. **Anche prima dei costi** le strategie sono a zero o negative su questo anno: −0,10 R in media; solo `rsi_reversion`
   leggermente positiva (+0,03 R, entro il rumore). Perdono anche *in-sample* (sui dati su cui sono state scelte).
3. **Le quattro regole d'uscita differiscono pochissimo** (al massimo 0,08 R per operazione, su `ema_crossover`). Su
   `bollinger_reversion` e `zscore_reversion` coincidono quasi esattamente: su barre orarie la maggior parte delle
   operazioni si chiude (stop, target o segnale) prima della mattina successiva, quindi la regola della mattina tocca poche
   operazioni (deduzione dai numeri; non ho contato le operazioni aperte alla mattina).
   - Chiudere **solo i vincenti** la mattina dopo (la tua regola) è marginalmente migliore delle uscite di sistema per
     trend e breakout (+0,04–0,06 R su `volume_breakout` ed `ema_crossover`), peggiore per `rsi_reversion` (−0,03 R): toglie
     i vincenti che sarebbero diventati più grandi e tiene i perdenti.
   - Il **time stop per tutti** è il migliore su `ema_crossover` (+0,08 R) e il peggiore su `rsi_reversion` (−0,06 R).
   - Nessuna regola cambia il segno del risultato: il problema non sono le uscite.
4. **Profilo crypto (dopo vs prima, fuori campione):** migliora l'aspettativa per operazione dove agisce sulle uscite di
   trend/breakout (`ema_crossover` −0,65 → −0,37 R, `volume_breakout` −0,53 → −0,32 R), è neutro su `rsi_reversion` e
   `zscore_reversion` (meno operazioni, stessa aspettativa), peggiora `bollinger_reversion` (−0,42 → −0,46 R). Il profit
   factor scende ovunque e il risultato resta negativo: **non va attivato per guadagnare**, al massimo per perdere meno
   operazioni di trend/breakout. In EUR per operazione la differenza è piccola perché la dimensione reale è limitata dal
   tetto di esposizione (25% per strumento), non dal rischio del 2%.
5. **Cosa conviene fare** (ordine di impatto):
   - **ridurre i costi in R**: timeframe più lunghi (stop più larghi in % → costi più piccoli in R), ingressi limit
     (commissione maker 0,15% e niente spread: già disponibili nel profilo, `maker_entries`), meno operazioni;
   - non usare queste strategie su crypto orario con commissioni taker;
   - se mantieni la chiusura manuale dei vincenti, la pagina Analytics ora misura con gli shadow exit quanto ti costa o ti
     rende sui tuoi trade veri.

## Come leggere le tabelle

- **R** = risultato / rischio iniziale (distanza dello stop all'ingresso × quantità). Step 4: EUR = R × 20 € (2% di 1.000 €),
  un valore nominale. Step 5: EUR come nel backtest della piattaforma (dimensioni reali, limitate dal tetto di esposizione).
- **Max DD** somma in sequenza *tutte* le operazioni di tutte le coppie e strategie come se fossero un unico conto: misura
  la profondità della serie, non il drawdown di un conto reale (che avrebbe operato meno in parallelo).
- **In-sample (IS)** = la combinazione scelta sui dati su cui è stata scelta; **fuori campione (OOS)** = il periodo
  successivo, mai visto. Sono riportati separati.
- **Regole d'uscita (step 4)**, sulle stesse entrate fuori campione:
  1. solo le uscite del sistema (stop, take profit, segnale della strategia);
  2. tutte chiuse all'apertura della prima barra dalle 09:00 Europe/Rome del giorno dopo la data d'ingresso (stop e target
     restano attivi fino ad allora);
  3. alla stessa ora chiuse solo se in guadagno, le altre proseguono con le uscite del sistema;
  4. metà chiusa a quell'ora, sul resto uno stop mobile di 2 ATR (ATR all'ingresso) più stop originale e segnale.
  Un'operazione che il sistema aveva già chiuso prima della mattina ha lo stesso risultato in tutte le regole.


Generated 2026-11-19 14:16 UTC by `python -m app.scripts.exit_study`.

## Data

| Pair | Hourly bars | From | To |
|---|---|---|---|
| BTC/USD | 8760 | 2025-11-19 14:00 | 2026-11-19 14:00 |
| ETH/USD | 8760 | 2025-11-19 14:00 | 2026-11-19 14:00 |
| SOL/USD | 8760 | 2025-11-19 14:00 | 2026-11-19 14:00 |
| LTC/USD | 8760 | 2025-11-19 14:00 | 2026-11-19 14:00 |
| LINK/USD | 8760 | 2025-11-19 14:00 | 2026-11-19 14:00 |
| AVAX/USD | 8760 | 2025-11-19 14:00 | 2026-11-19 14:00 |

## Step 4: four exit rules on the same out-of-sample entries

Entries: every out-of-sample trade of the walk-forward of the five strategies on the six pairs (strategies as they are, long-only). R after costs; EUR at 2% of 1000 EUR per R (20 EUR).

| Rule | Trades | Expectancy before costs (R) | Costs (R) | Expectancy (R) | Expectancy (EUR) | Profit factor | Max DD (R) | Total (R) |
|---|---|---|---|---|---|---|---|---|
| 1 system exits only | 1612 | -0.107 | 0.451 | -0.558 | -11.15 | 0.26 | 901.24 | -898.89 |
| 2 time stop next morning (all) | 1612 | -0.101 | 0.451 | -0.552 | -11.04 | 0.24 | 892.08 | -889.72 |
| 3 winners closed next morning | 1612 | -0.098 | 0.451 | -0.548 | -10.97 | 0.25 | 886.25 | -883.90 |
| 4 half next morning + ATR trail | 1612 | -0.099 | 0.451 | -0.549 | -10.98 | 0.25 | 887.73 | -885.38 |

### By strategy

**bollinger_reversion**

| Rule | Trades | Before costs (R) | Expectancy (R) | PF | Max DD (R) |
|---|---|---|---|---|---|
| 1 system exits only | 503 | -0.085 | -0.503 | 0.22 | 256.56 |
| 2 time stop next morning (all) | 503 | -0.086 | -0.504 | 0.22 | 257.04 |
| 3 winners closed next morning | 503 | -0.085 | -0.503 | 0.22 | 256.31 |
| 4 half next morning + ATR trail | 503 | -0.085 | -0.503 | 0.22 | 256.50 |

**ema_crossover**

| Rule | Trades | Before costs (R) | Expectancy (R) | PF | Max DD (R) |
|---|---|---|---|---|---|
| 1 system exits only | 249 | -0.249 | -0.759 | 0.28 | 188.97 |
| 2 time stop next morning (all) | 249 | -0.169 | -0.680 | 0.24 | 169.31 |
| 3 winners closed next morning | 249 | -0.193 | -0.703 | 0.25 | 175.16 |
| 4 half next morning + ATR trail | 249 | -0.178 | -0.689 | 0.27 | 171.50 |

**rsi_reversion**

| Rule | Trades | Before costs (R) | Expectancy (R) | PF | Max DD (R) |
|---|---|---|---|---|---|
| 1 system exits only | 210 | 0.034 | -0.366 | 0.39 | 77.76 |
| 2 time stop next morning (all) | 210 | -0.029 | -0.429 | 0.32 | 90.92 |
| 3 winners closed next morning | 210 | 0.000 | -0.400 | 0.34 | 84.86 |
| 4 half next morning + ATR trail | 210 | -0.011 | -0.412 | 0.34 | 87.33 |

**volume_breakout**

| Rule | Trades | Before costs (R) | Expectancy (R) | PF | Max DD (R) |
|---|---|---|---|---|---|
| 1 system exits only | 219 | -0.143 | -0.641 | 0.30 | 140.45 |
| 2 time stop next morning (all) | 219 | -0.115 | -0.613 | 0.30 | 134.31 |
| 3 winners closed next morning | 219 | -0.099 | -0.598 | 0.32 | 130.89 |
| 4 half next morning + ATR trail | 219 | -0.111 | -0.610 | 0.31 | 133.51 |

**zscore_reversion**

| Rule | Trades | Before costs (R) | Expectancy (R) | PF | Max DD (R) |
|---|---|---|---|---|---|
| 1 system exits only | 431 | -0.101 | -0.555 | 0.21 | 241.05 |
| 2 time stop next morning (all) | 431 | -0.108 | -0.562 | 0.20 | 244.04 |
| 3 winners closed next morning | 431 | -0.105 | -0.559 | 0.21 | 242.59 |
| 4 half next morning + ATR trail | 431 | -0.104 | -0.559 | 0.21 | 242.45 |

## Step 5: walk-forward before / after the crypto profile (out of sample apart from in sample)

| Strategy | Profile | IS trades | IS mean return | IS mean PF | OOS trades | OOS mean return | OOS expectancy (R) | OOS expectancy (EUR) | OOS PF | OOS max DD (EUR) | Cost / trade (EUR) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| bollinger_reversion | before | 1881 | -0.0672 | 0.63 | 503 | -0.0364 | -0.421 | -1.74 | 0.36 | 897.60 | 1.67 |
| bollinger_reversion | after | 749 | -0.0487 | 0.32 | 354 | -0.0288 | -0.457 | -1.95 | 0.28 | 700.44 | 1.68 |
| ema_crossover | before | 1070 | -0.0330 | 0.77 | 249 | -0.0229 | -0.650 | -2.23 | 0.40 | 555.47 | 1.67 |
| ema_crossover | after | 1284 | -0.0522 | 0.60 | 302 | -0.0203 | -0.366 | -1.62 | 0.36 | 488.94 | 1.41 |
| rsi_reversion | before | 741 | -0.0370 | 0.65 | 210 | -0.0096 | -0.285 | -1.10 | 0.58 | 236.88 | 1.68 |
| rsi_reversion | after | 288 | -0.0155 | 0.50 | 162 | -0.0078 | -0.286 | -1.15 | 0.51 | 216.52 | 1.66 |
| volume_breakout | before | 883 | -0.0427 | 0.62 | 219 | -0.0169 | -0.531 | -2.00 | 0.41 | 438.29 | 1.67 |
| volume_breakout | after | 1021 | -0.0545 | 0.51 | 255 | -0.0193 | -0.315 | -1.81 | 0.34 | 462.82 | 1.46 |
| zscore_reversion | before | 1496 | -0.0571 | 0.62 | 431 | -0.0322 | -0.463 | -1.79 | 0.36 | 787.84 | 1.69 |
| zscore_reversion | after | 624 | -0.0330 | 0.42 | 313 | -0.0254 | -0.463 | -1.95 | 0.28 | 612.35 | 1.68 |

Combinations tested per walk-forward run (each fold re-chooses among them on its training window): bollinger_reversion 8, ema_crossover 8, rsi_reversion 8, volume_breakout 8, zscore_reversion 8; 6 pairs x 2 (before/after) runs per strategy, 4 folds each.

Profile used for "after" (fixed, not optimised): `{"enabled": true, "regime_filter": "trend_only", "regime_ema": 200, "regime_adx_min": 25.0, "vol_entry": true, "vol_entry_atr": 1.0, "reversion_exit_at_mean": true, "time_stop_bars": 48, "trend_partial": true, "partial_fraction": 0.5, "partial_at_r": 1.0, "trailing_atr": 2.0, "breakout_confirm": true, "breakout_volume_mult": 1.5, "breakout_lookback": 20, "maker_entries": false}`

## Assumptions

- `taker_fee` = 0.0025
- `maker_fee` = 0.0015
- `spread_bps` = 10.0
- `slippage_bps` = 5.0
- `usd_eur` = 0.92
- `capital_eur` = 1000.0
- `risk_per_trade` = 0.02
- `funding_rate_8h` = 0.0
- `morning` = 09:00
- `morning_tz` = Europe/Rome
- `trailing_atr` = 2.0
- `history_days` = 365
- `folds` = 4

## Cosa non ho potuto verificare, o richiede dati che non ci sono

- **Azioni ed ETF**: lo storico intraday Alpaca delle azioni richiede le chiavi, non disponibili qui. Gli step 4 e 5 sono solo
  sulle crypto.
- **I tuoi trade reali** (e le tue chiusure manuali): il registro è sul tuo Mac. La pagina Analytics li misura in R, con gli
  shadow exit, dopo l'aggiornamento (`Update shadows & backfill`).
- **Commissioni Alpaca crypto**: ho ipotizzato 0,25% taker / 0,15% maker (livello di volume più basso). Da verificare sul
  listino attuale: sono il fattore che pesa di più.
- **Spread e slippage storici**: non esiste uno storico bid/ask; ipotizzati 10 bps di spread e 5 bps di slippage per lato.
- **Funding**: Alpaca non ha perpetual; il calcolo del funding (motore, simulatore, registro) è implementato e testato,
  ma senza una fonte di tassi reali vale 0 e non è stato confrontato con tassi veri.
- **Volume**: il volume crypto di Alpaca è quello della sola sua piattaforma (es. 0,19 BTC in un'ora): la conferma di volume
  dei breakout lavora su un volume poco rappresentativo del mercato.
- **Ingressi maker**: esclusi dallo studio. Il simulatore esegue un limit quando il prezzo lo attraversa, senza coda: è
  ottimista. Da misurare in paper.
- **Un solo anno** (nov 2025 – nov 2026) e un solo timeframe (1h): un altro regime di mercato può dare altri numeri.
- **Ordine dentro la barra**: con stop e target nella stessa barra conta lo stop (prudente); la chiusura parziale nel
  backtest è valutata sulla chiusura della barra, non sul massimo.
- **Shadow exit sui trade storici**: ricostruibili solo dove esistono ordini protettivi registrati o le impostazioni della
  strategia e le barre; gli altri sono marcati "not reconstructible".
- **Cambio USD/EUR**: un tasso fisso (0,92) per tutto il periodo nello studio; nella piattaforma si usa il tasso BCE del
  giorno di chiusura. I risultati in R non ne dipendono.
