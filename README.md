# CODSync WooCommerce

Independent Frappe backend for reconciling WooCommerce cash-on-delivery orders with courier payouts. It owns its backend code, schema, settings, roles and scheduled jobs. Installing the Shopify `codsync` app is not required. The custom frontend is a later phase.

## Backend scope

- WC COD Parcels, couriers, remittance batches/lines, exceptions and dispute stage logs.
- Expected freight/COD fee/GST, Paid/Short-paid/Missing decisions and normalized AWB matching.
- CSV/XLSX/XLS import preview and confirmation, including queued large imports.
- Dispute claims, reminders, refunds and notes; tracking adapters and scheduled sync.
- Currency conversion/rates and isolated cache keys.
- Aging, outstanding, payout reconciliation and unmatched-line reports.
- Paginated WooCommerce REST v3 order sync and signed order webhooks.
- Role-gated APIs for the future frontend in `codsync_woocommerce.codsync_woocommerce.ui_api`.

## Independence

All application DocTypes begin with `WC ` and belong to the `CODSync WooCommerce` module. App roles are `CODSync WooCommerce Manager`, `CODSync WooCommerce Operator` and `CODSync WooCommerce Viewer`. Frappe's own Currency, User, File and email facilities are used normally. No Shopify App Bridge, Shopify authentication or Shopify credentials are included.

## WooCommerce connection

Configure **WC Store Connection** with the full HTTPS store URL (including a WordPress subdirectory when applicable), a consumer key and consumer secret with order-read access, and the webhook signing secret. All three credentials are Password fields. API credentials are sent as HTTP Basic authentication over HTTPS, never as URL query parameters; redirects are not followed with credentials.

The API version is `wc/v3`. `cod_payment_methods` is a comma-separated list of COD gateway IDs, defaulting to `cod`.

Configure these order webhooks in WooCommerce, with the same delivery URL and signing secret:

- `order.created`
- `order.updated`
- `order.deleted`

Delivery URL:

```text
https://<frappe-site>/api/method/codsync_woocommerce.codsync_woocommerce.api.webhook
```

Order events require a matching `X-WC-Webhook-Source` and a valid base64 HMAC-SHA256 signature over the raw body. WooCommerce's initial `webhook_id=<id>` registration ping is acknowledged without reading records or queueing work. Event processing is queued after the request commits. Deleted orders are marked Cancelled; ledger records are retained.

Numeric WooCommerce order IDs are the stable parcel identifiers, independently of custom display order numbers. Older updates cannot replace a record with a newer stored source modification time. Upserts preserve settlement and reconciliation fields.

Manual/background sync uses pages of 100 and a UTC modification window with a one-second overlap. The successful-sync cursor advances only when every row succeeds; failed rows roll back to a savepoint. A failed HTTP page leaves the cursor unchanged. Re-running a window updates the same records rather than creating new ones.

## Tracking and delivery

The default tracking metadata key is `_wc_shipment_tracking_items`, supporting shipment-tracking entries with `tracking_number`, `tracking_provider` or `custom_tracking_provider`. Flat metadata keys for tracking number/provider, declared weight in kg and delivered date are configurable per connection.

A WooCommerce **completed** status means the store completed the order; it is not treated as proof of courier delivery. Delivery requires explicit delivered-date metadata or a tracking update. Product weights are not guessed from order totals or quantities. Courier API tracking uses the copied Ship24 adapter; the Mock adapter is for development/testing.

## Installation and testing

Use a separate development/test site first. Do not install on or migrate the Shopify site to validate this backend.

```bash
bench --site <woocommerce-site> install-app codsync_woocommerce
bench --site <woocommerce-site> migrate
bench --site <woocommerce-test-site> run-tests --app codsync_woocommerce
```

Database-free connector/schema tests can run from the bench `sites` directory:

```bash
../env/bin/python -m unittest \
  codsync_woocommerce.tests.test_woocommerce \
  codsync_woocommerce.tests.test_schema \
  codsync_woocommerce.tests.test_api_options \
  codsync_woocommerce.tests.test_scheduler_errors
```

The app includes independently copied lifecycle, importer, matching, reconciliation, currency, dispute and permission integration tests. Those require this app to be installed on a configured Frappe test site. No real store credentials or network calls are required by the connector unit tests.

Official contracts: [WooCommerce REST v3](https://woocommerce.github.io/woocommerce-rest-api-docs/) and [webhook documentation](https://woocommerce.com/document/webhooks/).
