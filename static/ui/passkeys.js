// Passkeys (WebAuthn) on the staff verify step and the Security page. A static file loaded under the
// page's CSP nonce: no inline script, no eval, nothing written into the page as HTML.
//
// A form marked data-passkey="login" or "register" runs one ceremony when it is submitted:
//   1. POST its data-options-url (the form's CSRF token in X-CSRFToken) for the options, as JSON;
//   2. ask the browser's authenticator (navigator.credentials.get / create);
//   3. put the answer, as JSON, in the form's hidden "credential" field and submit the form.
// The server checks everything (challenge, origin, RP ID, user verification, counter); this file
// decides nothing. Without WebAuthn the login form stays hidden and "Add a passkey" stays disabled.
(function () {
  "use strict";

  var forms = document.querySelectorAll("form[data-passkey]");
  if (!forms.length) { return; }
  var supported = !!(window.PublicKeyCredential && navigator.credentials && window.fetch);

  function each(nodes, fn) { Array.prototype.forEach.call(nodes, fn); }

  each(document.querySelectorAll("[data-passkey-unsupported]"), function (el) { el.hidden = supported; });
  if (!supported) { return; }
  each(document.querySelectorAll("[data-passkey-or]"), function (el) { el.hidden = false; });

  // ---- base64url <-> ArrayBuffer: the options arrive as JSON, the answer leaves as JSON.
  function toBuffer(text) {
    var base64 = String(text).replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) { base64 += "="; }
    var binary = atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) { bytes[i] = binary.charCodeAt(i); }
    return bytes.buffer;
  }
  function toText(buffer) {
    if (!buffer) { return null; }
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) { binary += String.fromCharCode(bytes[i]); }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function descriptors(list) {
    return (list || []).map(function (c) {
      var d = { type: c.type, id: toBuffer(c.id) };
      if (c.transports) { d.transports = c.transports; }
      return d;
    });
  }
  function creationOptions(o) {
    o.challenge = toBuffer(o.challenge);
    o.user.id = toBuffer(o.user.id);
    o.excludeCredentials = descriptors(o.excludeCredentials);
    return o;
  }
  function requestOptions(o) {
    o.challenge = toBuffer(o.challenge);
    o.allowCredentials = descriptors(o.allowCredentials);
    return o;
  }
  function common(credential) {
    return {
      id: credential.id, rawId: toText(credential.rawId), type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment || null,
      clientExtensionResults: typeof credential.getClientExtensionResults === "function" ? credential.getClientExtensionResults() : {}
    };
  }
  function registrationJSON(credential) {
    var out = common(credential);
    var r = credential.response;
    out.response = {
      clientDataJSON: toText(r.clientDataJSON), attestationObject: toText(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : []
    };
    return out;
  }
  function assertionJSON(credential) {
    var out = common(credential);
    var r = credential.response;
    out.response = {
      clientDataJSON: toText(r.clientDataJSON), authenticatorData: toText(r.authenticatorData),
      signature: toText(r.signature), userHandle: toText(r.userHandle)
    };
    return out;
  }

  // ---- what the person is told
  function say(form, text, isError) {
    var out = form.querySelector("[data-passkey-status]");
    if (!out) { return; }
    out.textContent = text || "";
    out.classList.toggle("is-error", !!isError);
  }
  function serverSays(text) {
    var error = new Error(text || "The server refused. Reload the page and try again.");
    error.name = "ServerMessage";
    return error;
  }
  function explain(error, mode) {
    switch (error && error.name) {
      case "ServerMessage": return error.message;
      case "NotAllowedError": return "Cancelled, or the passkey did not answer in time. Try again.";
      case "AbortError": return "Cancelled. Try again.";
      case "InvalidStateError":
        return mode === "register" ? "This authenticator already holds a passkey for this account." : "That passkey cannot be used here.";
      case "SecurityError": return "The browser refused: this address is not the one passkeys are set up for.";
      case "NotSupportedError": return "This authenticator cannot make a passkey this site accepts.";
      default: return "Something went wrong (the server could not be reached, or its answer was not understood). Try again.";
    }
  }
  function samePath(path) { return typeof path === "string" && /^\/(?![\/\\])/.test(path); }

  function run(form) {
    var mode = form.getAttribute("data-passkey");
    var button = form.querySelector("button[type='submit']");
    var token = form.querySelector("input[name='csrfmiddlewaretoken']");
    var field = form.querySelector("input[name='credential']");
    field.value = "";
    button.disabled = true;
    say(form, mode === "register" ? "Follow your browser's prompt to create the passkey." : "Follow your browser's prompt to use your passkey.");
    fetch(form.getAttribute("data-options-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": token ? token.value : "", "Accept": "application/json" }
    }).then(function (response) {
      return response.json().then(
        function (body) { return { ok: response.ok, body: body || {} }; },
        function () { return { ok: false, body: {} }; });
    }).then(function (answer) {
      if (!answer.ok) {
        var back = answer.body.verify || answer.body.login;
        if (samePath(back)) { window.location.assign(back); return null; }
        throw serverSays(answer.body.error);
      }
      if (mode === "register") {
        return navigator.credentials.create({ publicKey: creationOptions(answer.body) }).then(registrationJSON);
      }
      return navigator.credentials.get({ publicKey: requestOptions(answer.body) }).then(assertionJSON);
    }).then(function (answer) {
      if (!answer) { return; }
      field.value = JSON.stringify(answer);
      say(form, "Checking the passkey.");
      HTMLFormElement.prototype.submit.call(form);
    }).catch(function (error) {
      button.disabled = false;
      say(form, explain(error, mode), true);
    });
  }

  each(forms, function (form) {
    if (form.getAttribute("data-passkey") === "login") { form.hidden = false; }
    each(form.querySelectorAll("[data-passkey-start]"), function (button) { button.disabled = false; });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      run(form);
    });
  });

  // Back to a page kept in the browser's cache: its button was left disabled mid-ceremony.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) { return; }
    each(forms, function (form) {
      each(form.querySelectorAll("button[type='submit']"), function (button) { button.disabled = false; });
      say(form, "");
    });
  });
})();
