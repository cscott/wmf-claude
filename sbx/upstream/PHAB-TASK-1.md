Title: MediaWiki-REST-API: Header parameters are looked up by exact case, so `POST /page` with a lowercase `content-type` fails

## Summary

`ParamValidatorCallbacks` finds a `'source' => 'header'` parameter with `$params[$name]` on the array from `RequestInterface::getHeaders()`, which is keyed by the header name as the client sent it. HTTP header names are case-insensitive (RFC 9110 §5.1), so a client that sends `content-type: application/json` to `POST /page` or `PUT /page/{title}` gets `400` with `The "Content-Type" parameter must be set.`

## Technical notes

This started when core commit 0ca91a896ad (T412668) declared `Content-Type` as a header parameter of the page create and update handlers. Whether it shows depends on the web server: `php -S` (what `composer serve` and MediaWiki Quickstart run) keeps header names as the client sent them. The `api-testing` npm client sends lowercase names, so core's own `tests/api-testing/REST/Creation.js` and `Update.js` fail against `composer serve`: 15 of 27 tests (17 in the full `npm run api-testing`), all `expected 400 to equal 201` (or 403, 409). `curl -H 'content-type: application/json'` reproduces it.

The attached patch (PHAB-ATTACHMENT-1.patch) uses `RequestInterface::hasHeader()` and `getHeader()` for the header source, which `HeaderContainer` already makes case-insensitive, and adds a test case for a header sent as `PARAM1` and read as `param1`. Measured on core 12be50619e4 under `composer serve`: `tests/phpunit/unit/includes/Rest/` 674 tests pass; the new case fails without the fix; `Creation.js` and `Update.js` go from 12 passing / 15 failing to 27 passing.

## Acceptance criteria

- [ ] A header parameter matches whatever case the client uses for the header name, in both `hasParam()` and `getValue()`.
- [ ] Core's REST api-testing suites pass against `composer serve`.
