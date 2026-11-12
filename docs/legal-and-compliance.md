# Legal, regulatory and operational considerations

This document is engineering guidance, **not legal advice**. It assumes an EU-based individual user
trading their own money. Confirm everything with a qualified adviser for your jurisdiction.

## What this software is

A self-hosted technical platform for research, simulation and order automation on the user's own
accounts. It gives no investment advice, makes no performance promise and holds no client funds.
Simulated results (backtests, paper trading, model scores) are not evidence of future returns;
the interface and API state this wherever such results appear.

## Before any real-money use

Live trading is off and stays off until an operator deliberately configures a separate deployment.
Before doing that:

1. **Compliance review** for your jurisdiction and situation (see the checklist below).
2. **Technical validation that has NOT been done in this project:** run each broker adapter against
   the broker's own test environment (exchange testnet, IBKR paper gateway) for an extended period,
   including disconnects, partial fills, rejected orders and restarts, and reconcile every position
   and fee against the broker's statements. The adapters here were tested against mocks only.
3. **Operational readiness:** monitoring and alert delivery, backups of the database, a tested
   kill-switch procedure, and a person who watches the system.
4. Start with the smallest position sizes, human approval of every order (the default for real
   accounts) and the default emergency policy.

## Checklist

| Topic | What to check |
|---|---|
| Trading for others | Managing other people's money, offering signals or copy trading generally requires authorisation (MiFID II investment services; national rules). This platform is built for own-account use only. |
| Algorithmic trading rules | MiFID II Art. 17 and RTS 6 impose organisational requirements on investment firms using algorithms (testing, kill functionality, record keeping). Private individuals are usually outside their scope, but trading venues and brokers apply their own rules to API users. |
| Crypto-assets | MiCA governs crypto-asset service providers in the EU. Use providers authorised for your country; some products (e.g. crypto derivatives) are restricted for retail clients in several jurisdictions. |
| Leverage and derivatives | ESMA product-intervention measures cap leverage for retail CFD clients; futures and margin trading need the corresponding account permissions and suitability checks at the broker. |
| Market abuse | Order patterns that could be seen as manipulation (layering, spoofing, wash trades) are prohibited (MAR). The platform places only orders arising from its strategies and never cancels to mislead, but responsibility remains with the account holder. |
| Broker and exchange terms | API terms of use, rate limits, order-to-trade ratios, and restrictions on automated trading. |
| Market-data licensing | Exchange and vendor data is licensed. Real-time data often may not be stored, redistributed or displayed to others without a licence; delayed and end-of-day data have their own terms. The platform labels data provenance but cannot check your licences. |
| Tax | Automated trading can create many taxable events. Keep the exported trade history; reporting obligations (including DAC8 for crypto) are yours. |
| Privacy (GDPR) | The database holds user emails, password hashes, audit trails and encrypted API credentials. If anyone other than you uses the deployment you are a data controller: secure the host, limit access, define retention, and be able to delete a user's data. |
| Security | Use API keys restricted to reading and trading, never withdrawal (the platform refuses keys it can see are withdrawal-enabled, but cannot verify every exchange); IP-restrict keys where the broker allows; keep `.env` and database backups private; serve the dashboard over HTTPS with `QAT_COOKIE_SECURE=true`. |

## Known operational limits

See the limitations section of [roadmap.md](roadmap.md). In particular: no broker adapter has been
run against a real venue, exchange holidays are not bundled, and model outputs are research
artefacts that do not influence orders.
