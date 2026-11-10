# Manual de usuario de Sealbill

Sealbill es un sistema de facturación autoalojado para quienes aceptan criptomonedas, transferencias bancarias y enlaces de pago a la vez y quieren que los datos de los clientes estén cifrados en reposo. Este manual está pensado para las personas que lo utilizan: el personal que emite facturas y quien lo instala.

Idiomas: [English](../en/README.md) · [Slovenčina](../sk/README.md) · [Čeština](../cs/README.md) · [Deutsch](../de/README.md) · **Español** · [Français](../fr/README.md) · [Polski](../pl/README.md) · [한국어](../ko/README.md) · [Русский](../ru/README.md)

## Capítulos

1. [Primeros pasos](01-getting-started.md): instalación, la bóveda, inicio de sesión, la reconfirmación para cambios sensibles
2. [Ajustes](02-settings.md): organizaciones, cuentas bancarias, canales de pago (rails), tokens de API, webhooks, el equipo
3. [Contactos](03-contacts.md), incluidas las claves de cifrado por contacto
4. [Facturas](04-invoices.md): crear, enviar, las páginas del cliente, cotizaciones, pagos parciales y excesos, anular, nuevos enlaces, el recordatorio
5. [Enlaces de pago](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (lo que necesita a una persona)
7. [Migración desde Zaprite y Request Finance](07-migration.md)
8. [Exportaciones y la moneda contable](08-exports.md)
9. [La API en resumen](09-api.md)

## Cómo leer las capturas de pantalla

- Las **páginas del personal** (todo lo que está tras el inicio de sesión) están siempre en inglés, hable el cliente el idioma que hable. Los nombres de menús y botones aparecen en este manual exactamente como los muestra la aplicación.
- Las **páginas del cliente** (la página de la factura `/i/…`, la página de pago `/p/…`, las páginas de los enlaces de pago, la página de desbloqueo y los correos) están traducidas a en, sk, cs, de, es, fr, pl, ko y ru. El cliente ve el idioma del contacto, luego el de la organización y luego el de su navegador; añada `?lang=de` a un enlace de cliente para verlo en otro idioma.
- Todos los nombres, direcciones de correo, carteras e importes de las capturas son inventados: proceden de una organización de demostración, «Example Studio s.r.o.», en una base de datos desechable.
- El texto en `código` es lo que usted escribe o copia. Los comandos se muestran como `manage.py …`; con Docker Compose es `docker compose exec web python manage.py …` y con systemd `payinv-manage …` (véase el README del repositorio).
