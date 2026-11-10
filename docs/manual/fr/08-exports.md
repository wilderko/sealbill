# 8. Exports et devise comptable

**Exports** dans la barre donne deux fichiers CSV pour la comptabilité.

![Exports](../img/exports.png)

Choisissez les organisations (aucune cochée signifie toutes), des dates **From** et **To** facultatives, et téléchargez :

- **invoices.csv**, une ligne par facture ;
- **payments.csv**, une ligne par paiement, avec son état.

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | qui et quand |
| `subtotal, discount, tax, total, paid` | dans la devise de la facture |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | le même total dans la devise comptable de l'organisation |
| `external_reference, payment_link, migrated_from` | d'où elle vient : la référence de votre système, un lien de paiement ou le service depuis lequel elle a été migrée |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | ce qui est arrivé et comment, dans l'actif reçu |
| `invoice_currency, amount_invoice_currency, quote_rate` | le montant dans la devise de la facture et le taux du devis avec lequel on a payé |
| `accounting_currency, accounting_rate, accounting_amount` | la même chose dans la devise comptable |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | la preuve : la transaction, quand elle a été vue et confirmée, qui l'a confirmée |

## La devise comptable

Chaque organisation a une **devise comptable** (Settings > Organizations ; EUR par défaut) : la devise dans laquelle elle tient ses livres. Les colonnes comptables convertissent chaque montant dans cette devise avec **le taux mis en cache pour le jour** : la date d'émission pour les factures et la date de confirmation pour les paiements. Le worker remplit le cache toutes les heures (`accounting_rates`). Un jour sans taux en cache laisse ces cellules **vides** ; `rate_source` dit d'où vient un taux (`identity` quand les devises sont identiques). Un export n'appelle jamais de service de taux, il est donc rapide et ne change pas si vous le relancez.

Les mêmes fichiers sont disponibles par l'API pour un jeton avec `read` : `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31` et `GET /api/v1/exports/payments.csv?…`, 400 jours au plus à la fois.
