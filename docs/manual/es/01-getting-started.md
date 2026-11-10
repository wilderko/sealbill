# 1. Primeros pasos

## Qué está ejecutando

Sealbill son una aplicación web y un worker en segundo plano.

- La **aplicación web** sirve las páginas del personal (en la raíz del sitio, por ejemplo `https://pay.example.org/`), las páginas del cliente (`/i/…` muestra una factura, `/p/…` es donde se paga, `/l/…` es un enlace de pago), la API (`/api/v1/…`) y la página de desbloqueo (`/unlock/`).
- El **worker** (`manage.py run_worker`) hace todo lo que debe ocurrir sin un clic: envía el correo en cola, marca las facturas como vencidas, vigila las cadenas y las pasarelas en busca de pagos, hace caducar las cotizaciones, envía el único recordatorio de pago y entrega los webhooks. **Sin el worker no se envía ningún correo y no se detecta ningún pago en cripto.** Ejecútelo como segundo proceso junto a la aplicación web; `manage.py run_worker --once` ejecuta cada tarea una vez y termina, lo que viene bien para una prueba.

## Instalación y primer arranque

El README del repositorio tiene los comandos para cada forma de ejecutarlo, así que no se repiten aquí:

- en su propio equipo, para probarlo: [README, Development](../../../README.md#development)
- con Docker Compose: [README, Docker Compose](../../../README.md#docker-compose)
- en un servidor con systemd, Apache y PostgreSQL: [README, Deploying with systemd and Apache](../../../README.md#deploying-with-systemd-and-apache)

Elija lo que elija, el orden del primer arranque es el mismo:

1. Cree las tablas de la base de datos (`migrate`, y `createcachetable` para las tablas de límites de peticiones).
2. Cree la bóveda y desbloquéela (siguiente apartado).
3. Cree el primer usuario con `manage.py createsuperuser`.
4. Dele un segundo factor: `manage.py create_staff_totp <username>` imprime una dirección `otpauth://` para una aplicación de autenticación. Requiere la bóveda desbloqueada. Puede añadir una passkey más tarde en la página Security.
5. Inicie la aplicación web y el worker, abra el sitio e inicie sesión.
6. En **Settings** cree una organización y una cuenta bancaria o una cuenta de canal de pago (capítulo 2), y envíese una primera factura a usted mismo (capítulo 4).

## La bóveda

Todo lo que una persona ha escrito (nombres, direcciones, identificadores fiscales, líneas de factura, notas, credenciales de sus pasarelas de pago) se guarda cifrado. La clave se deriva de una **contraseña de la bóveda** y se conserva solo en el archivo en memoria que indica `VAULT_KEY_PATH`; nunca está en el entorno ni en el disco.

```bash
printf '%s\n' 'a long vault password' | manage.py init_vault      # una vez; al menos 16 caracteres
printf '%s\n' 'a long vault password' | manage.py unseal_vault    # tras cada reinicio de la máquina
```

Escriba la contraseña por la entrada estándar, como arriba, para que nunca aparezca en una línea de comandos ni en el historial del shell. `init_vault` solo crea `vault.json` (la sal y un valor de comprobación, no la clave); `unseal_vault` la abre.

Tras reiniciar la máquina, o si se borra el archivo en memoria, la bóveda queda **sellada**. Una recarga suave del servicio web la deja abierta; un reinicio brusco, no. Mientras está sellada:

- toda página del personal redirige a **/unlock/**: introduzca allí la contraseña de la bóveda o ejecute `unseal_vault`;
- las páginas de pago, los enlaces de pago y la API responden «temporalmente no disponible» (503), de modo que ningún cliente puede obtener una cotización;
- el worker sigue viendo y casando los pagos en cadena (solo lee columnas sin cifrar), pero el correo y los webhooks esperan y el sondeo de las pasarelas (BTCPay, Lightning) se detiene; quien opera el sistema recibe un correo de alerta por cada episodio de sellado.

![La página de desbloqueo, mostrada cuando la bóveda está sellada](../img/vault-unlock.png)

`GET /healthz` informa `"vault": "sealed"` o `"unsealed"` sin ningún dato, de modo que un monitor puede vigilarlo. Guarde una copia de `vault.json`, la frase de la copia de seguridad y la contraseña de la bóveda en un sitio distinto del servidor: una base de datos sin las tres cosas no se puede leer. La lista completa de lo que sigue funcionando con la bóveda sellada está en el [README](../../../README.md#what-runs-while-the-vault-is-sealed).

## Iniciar sesión

El personal inicia sesión en `/login/` en dos pasos: la **contraseña** y luego un **segundo factor**, que es una passkey o un código de seis dígitos de una aplicación de autenticación. Una sesión dura ocho horas y termina al cerrar el navegador. El inicio de sesión tiene un límite de intentos por dirección, por cuenta y por dispositivo.

![El paso de la contraseña](../img/start-login.png)

![El paso del segundo factor](../img/start-verify.png)

Si la cuenta tiene una passkey, el segundo paso ofrece primero **Use a passkey** y debajo el campo del código («or enter a code»). Una passkey pertenece solo a este sitio, así que una página falsa no puede usarla.

![El segundo paso de una cuenta con passkey](../img/verify-passkey.png)

Tras iniciar sesión llega a **Needs a person** (capítulo 6). La barra de la izquierda es toda la navegación: Needs a person, Invoices, New invoice, Contacts, Payment links, Exports, Settings; el escudo de abajo es **Security**, la flecha cierra la sesión.

![Needs a person, la primera página tras iniciar sesión](../img/needs-person.png)

### Security: sus propias passkeys y autenticadores

**Security** (el icono del escudo) enumera sus segundos factores. Para añadir una passkey póngale nombre (por ejemplo «MacBook Touch ID» o «YubiKey 5C») y pulse **Add a passkey**; el navegador pedirá entonces la huella, el rostro o el PIN. Una aplicación de autenticación se añade en el servidor con `create_staff_totp`, nunca en el navegador. El último segundo factor de una cuenta no se puede eliminar, y quitar el que usó esta sesión pide antes otro. Cada alta y cada baja queda en el registro de auditoría.

![Security, con una passkey añadida](../img/security-passkey.png)

## Personal y superusuarios

Cualquier cuenta de personal puede emitir facturas, registrar pagos y atender las colas. Algunos cambios pueden mover dinero o redirigir a un cliente, y exigen más.

### La reconfirmación (step-up)

Estos cambios exigen un **superusuario** que haya dado un segundo factor **en los últimos diez minutos**:

- crear, cambiar los scopes o revocar un token de API;
- una cuenta de canal de pago y sus credenciales (y el webhook de BTCPay, que guarda un secreto);
- el indicador de emisor para residentes fiscales de EE. UU. de una organización y sus hosts de retorno;
- una cuenta bancaria o un enlace de pago fiat de una organización;
- crear o eliminar un webhook (adonde van los eventos de una organización);
- añadir una dirección de correo a un contacto o hacerla principal (a ella van los correos, los enlaces de pago y los PDF);
- eliminar el segundo factor de otro miembro del personal.

Las páginas dicen en qué punto está antes de que pulse nada:

- un miembro del personal que no es superusuario ve «Only a superuser may …» y no cambia nada;

  ![Un miembro del personal que no es superusuario](../img/stepup-not-superuser.png)

- un superusuario cuyo último segundo factor tiene más de diez minutos ve **Verify again**; lleva a la página de verificación y le devuelve a la página en la que estaba.

  ![Un cambio que necesita un segundo factor reciente](../img/stepup-reverify.png)

  ![Verify again](../img/stepup-verify.png)

Cada cambio con reconfirmación envía además un correo a todos los superusuarios activos: quién, qué y cuándo, con nombres e identificadores y nunca un secreto, un número de cuenta ni una dirección. Si una sesión robada cambia algo, el propietario del sistema se entera.

La página **Team** (Settings > Team) enumera a todos los que pueden iniciar sesión, sus segundos factores y el último inicio de sesión, y permite a un superusuario eliminar el factor perdido de un compañero. Los nuevos miembros del personal se crean con `createsuperuser` o en el Django admin oculto (la ruta de `DJANGO_ADMIN_PATH`; solo superusuario con segundo factor, todos los demás reciben un 404).
