# 5. Enlaces de pago

Un **enlace de pago** es una página, `/l/<token>/`, para un servicio a un precio fijo (o por un importe que elige el comprador) que pagan muchas personas: un botón de tienda, un código QR en un cartel, un enlace en un boletín. Abra **Payment links** en la barra.

(No son los *enlaces fiat* de una organización del capítulo 2, que solo apuntan a Revolut, PayPal y similares.)

![Enlaces de pago](../img/links.png)

## Crear un enlace

**New payment link** pregunta por:

- **What is sold**: la organización que vende, el documento (*Invoice* o *Payment request*), un título y una descripción que ven los compradores y una imagen opcional (PNG, JPEG o WebP de hasta 1,5 MB, recodificada; 960 × 540 encaja en la página).
- **Price**: moneda, precio e impuesto %, **Maximum quantity per order** (0 significa ilimitado, hasta 200), o marque **The customer chooses the amount** con mínimo y máximo opcionales.
- **What the buyer is asked**: la dirección de correo y «Are you a US tax resident?» se preguntan **siempre**; el nombre, la dirección de facturación, la de envío y el teléfono pueden ser off, optional o required; hasta cinco preguntas adicionales.
- **Payment methods**: los métodos por defecto del emisor o un **conjunto personalizado**, como «USDC on Arbitrum One», «Bank transfer in EUR» o «Bitcoin over Lightning». Un conjunto personalizado nombra tipos de pago, no cuentas, de modo que el emisor de EE. UU. ofrece sus propias cuentas de los mismos tipos.
- **Availability**: active, *Valid until*, *Total uses*, **Paid orders need fulfilling** y **E-mail staff for each paid order**, y una nota interna que solo ve el personal.

![El formulario de enlace nuevo](../img/link-new.png)

El enlace funciona en cuanto está guardado y activo.

## Compartirlo

La página del enlace muestra la URL con **Copy**, un **código QR** (PNG o SVG), una **hoja imprimible** (PDF), el HTML de un **botón para una página web** y **Replace the URL**, que da una dirección nueva y detiene la antigua al instante. Cualquiera con la URL puede comprar; añada `?promoCode=CODE` para rellenar un cupón.

![Un enlace, con su código QR, cupones y pedidos](../img/link-detail.png)

**Cupones**: un código, un porcentaje de descuento sobre la línea (antes de impuestos, nunca un pedido gratis), opcionalmente un número de usos y una fecha de fin. **Copy link with code** da una URL que lo rellena.

## Lo que ve el comprador

![La página pública de un enlace](../img/link-public.png)

El comprador escribe una dirección de correo, un nombre si se le pide, responde a la pregunta sobre EE. UU. y continúa al pago. La página a la que llega es la **página de pago** habitual del capítulo 4.

![La página de pago de un pedido](../img/link-checkout-pay.png)

- Cada checkout crea un **pedido**: una factura o una solicitud de pago con una línea, numerada y enviada al instante, con una instantánea congelada. **No se envía ningún correo antes del pago**; la página de pago dice que la conserve. La primera carta es el recibo, o la carta de pago parcial, y lleva los enlaces del pedido.
- **Residentes fiscales de EE. UU.** La respuesta que escribe el comprador decide qué organización emite el pedido: un yes al emisor de EE. UU., un no a la organización del enlace. Lo que guarda un contacto nunca decide, así que un enlace público no revela nada de lo guardado. Una respuesta escrita que el indicador guardado contradice se convierte en un elemento de Needs a person, y con un «no» frente a un «yes» guardado el pedido no se ofrece para su cumplimiento hasta que una persona lo compruebe con el comprador.
- **Un formulario público nunca muestra ni cambia un contacto.** Una dirección que coincide con un cliente registrado asigna el pedido a ese cliente. Si no, se crea un *link buyer* con los datos escritos. Los link buyers aparecen en Contacts con un filtro, ninguna búsqueda los encuentra y nada los reutiliza; una persona puede convertir uno en cliente registrado desde su página, una vez comprobado.

  ![Contacts, solo link buyers](../img/contacts-link-buyers.png)

## Pedidos, usos y límites

- Un checkout **retiene** uno de los usos del enlace (y del cupón) durante 60 minutos; el pago convierte la retención en un uso. Un pago que llega después de que caducara la retención conserva su precio; si eso hace que un enlace o cupón con límite lo supere, el personal recibe un elemento en Needs a person.
- **Los pedidos sin pagar se anulan a los 7 días.** El dinero que llegue después va a revisión, como con cualquier factura anulada.
- **Cumplimiento.** Con *Paid orders need fulfilling* activado, los pedidos pagados figuran en la página del enlace como por cumplir hasta que alguien pulsa **Mark fulfilled**.
- **Límites**: 5 intentos de checkout cada 10 minutos por dirección y 50 checkouts completados por hora por enlace. Ninguna página puede incrustar un enlace.
- Crear o editar un enlace por la API requiere `invoices:write`; la URL del enlace se entrega al crearlo, al rotarlo y al leer un enlace concreto con un token que también tiene `invoices:write` (un token solo `read` recibe null).
