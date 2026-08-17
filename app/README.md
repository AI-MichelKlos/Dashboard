# Danske A-kasser – Analyse

Denne mappe er en separat webapp/PWA, der samler adgang til de eksisterende dashboards.

## Princip

- Appen ændrer ikke de eksisterende dashboards eller deres dataflows.
- De enkelte dashboards fortsætter i deres egne repositories og med deres egne GitHub Actions.
- Appens service worker cacher kun filer under `/Dashboard/app/` og cacher ikke dashboarddata fra de øvrige projekter.
- Hvert dashboard kan både åbnes inde i appen og separat.

## Dashboards

- A-kasseindsigt
- JUR – Arbejdsmarkedsoverblik
- Analytisk overblik – Arbejdsmarkedet
- Udenlandske lønmodtagere i Danmark
- Ansatte i Danmarks største virksomheder

## PWA

`manifest.webmanifest` og `service-worker.js` gør appskallen installerbar på understøttede enheder. Forsiden kan vises fra cache, mens de levende dashboards fortsat hentes online, så deres aktuelle data ikke erstattes af en app-cache.
