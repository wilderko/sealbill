# 3. Contactos

Un **contacto** es un cliente de una organización: nombres, una o varias direcciones de correo, una dirección postal, identificadores fiscales, un teléfono, un idioma preferido, una nota y la respuesta a «US tax resident?». Cada contacto pertenece a una organización. Abra **Contacts** en la barra.

![La lista de contactos](../img/contacts.png)

- Busque por la **dirección de correo exacta**; filtre por organización.
- La lista muestra **Clients and link buyers**, **Clients of record** o **Link buyers**. Un *link buyer* es un contacto que creó el checkout de un enlace de pago a partir de lo que escribió un visitante; en un formulario público cualquiera puede escribir cualquier cosa, por eso no es un cliente registrado (client of record) hasta que una persona lo asciende (capítulo 5).
- Un contacto que ve dos veces con el mismo nombre, una bajo cada organización, es la **copia del cliente en el emisor de EE. UU.** (capítulo 2): el emisor guarda su propia copia para que los datos de la primera organización nunca se reescriban.

## Crear y cambiar un contacto

Pocas veces crea uno por separado. Al escribir una dirección de correo en **New invoice** se encuentra el contacto con esa dirección o se crea uno nuevo con lo escrito; lo que deje en blanco nunca sobrescribe lo guardado. También crean contactos la API y el importador.

**Contact > Edit** cambia el nombre, el teléfono, el idioma preferido, la respuesta sobre EE. UU., los identificadores fiscales, las notas y la dirección. La organización no se puede cambiar. Rebajar una respuesta guardada sobre EE. UU. (desde «yes») queda en el registro de auditoría.

![Editar un contacto](../img/contact-edit.png)

![Un contacto](../img/contact.png)

En la página del contacto, la lista **E-mail addresses** añade una dirección, marca una como **Primary** y elimina direcciones. Los correos, los enlaces de pago y los PDF van a la dirección principal, así que añadir o ascender una dirección es un cambio con [reconfirmación](01-getting-started.md). No se pueden eliminar la dirección principal, la última dirección ni una dirección que nombre una clave de cifrado activa.

## Claves de cifrado de un contacto

Todo lo guardado sobre un contacto ya está cifrado en reposo con la clave de la bóveda (capítulo 1). Un contacto puede tener además una clave propia, de modo que **el correo que usted le envía** se cifre con ella: una **clave pública PGP** o un **certificado S/MIME**. Abra **Contact > Encryption keys**.

![Un contacto sin clave](../img/contact-keys-empty.png)

- **Subida por el personal.** Pegue la clave o elija un archivo. Debe nombrar una de las direcciones del contacto, no estar caducada y ser una clave pública o un certificado (una clave privada o un archivo `.p12` se rechaza). Pasa a ser la clave **activa** de inmediato y sustituye a la actual; no se pide confirmación al cliente.
- **Subida por el cliente.** Cada página del cliente tiene **Recibir correo cifrado**. La clave que el cliente sube ahí queda **pending** (pendiente): espera hasta que el cliente responde a un correo cifrado con ella, lo que demuestra que posee la mitad privada. Si el contacto ya tenía una clave activa, el titular de esa clave recibe un aviso con un enlace para **cancelar**, de modo que quien lea el buzón no pueda colar su propia clave sin que nadie lo note.

  ![La página del cliente para subir una clave](../img/portal-key-upload.png)

- Solo se usa la clave **activa**, una por contacto. Su estado es `pending`, `active`, `replaced`, `revoked` o `failed`. **Revoke** la retira; el correo pasa entonces a enviarse sin cifrar.
- Una clave que **deja de funcionar estando activa** (caducó o fue revocada en origen) se marca `failed`, y el correo a ese contacto se **rechaza en lugar de enviarse sin cifrar**. Aparece en Needs a person bajo *Encryption keys that stopped working*; suba una clave que funcione o revoque la fallida, y luego reintente los mensajes. Una clave que falla aún en estado pending no detiene nada. Needs a person también enumera las *claves que caducan en 30 días*.
- La copia de un contacto en el emisor sigue automáticamente la clave del original.

![Un contacto con una clave PGP activa](../img/contact-keys-active.png)

Lo mismo puede hacerse por la API: `POST /api/v1/contacts/{id}/keys` con `{"key": "<armored key>"}` (scope `invoices:write`) y `POST /api/v1/keys/{id}/revoke`.
