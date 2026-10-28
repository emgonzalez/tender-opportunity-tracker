// Tablero del pipeline (E13): mover una oportunidad de columna ARRASTRANDO su tarjeta, y abrir
// los acordeones <details> a los que apunta un ancla.
//
// Mejora progresiva: el arrastre no crea rutas ni hace peticiones. Rellena el <form class="cambio">
// que la tarjeta ya trae («Mover a…») y lo envía con requestSubmit(): CSRF, el token de concurrencia
// `actualizado_en` y la redirección PRG siguen siendo los del servidor, que vuelve a validar todo.
// Sin JS (o en táctil, donde el DnD HTML5 no existe) «Mover a…» sigue siendo el camino.
//
// Solo lee el DOM con textContent, dataset y getAttribute; solo escribe con textContent,
// setAttribute, classList y createElement. Nunca interpreta como HTML lo que viene en data-*.
(function () {
  "use strict";

  var FINALES = { GANADO: true, PERDIDO: true, DESCARTADO: true };
  var NOMBRES = {
    NUEVO: "Nuevo", EVALUANDO: "Evaluando", OFERTADO: "Ofertado",
    GANADO: "Ganada", PERDIDO: "Perdida", DESCARTADO: "Descartada",
  };
  var CON_MONTO = { OFERTADO: true, GANADO: true };
  var CON_COMPETIDOR = { GANADO: true, PERDIDO: true };

  function csv(txt) {
    return String(txt || "").split(",").map(function (s) { return s.trim(); })
      .filter(function (s) { return s !== ""; });
  }

  function nombreEstado(e) { return NOMBRES[e] || e; }

  // ── Acordeones por ancla ────────────────────────────────────────────────────
  function abrirAncla(hash, desplazar) {
    try {
      if (!hash || hash.charAt(0) !== "#" || hash.length < 2) return;
      var id;
      try { id = decodeURIComponent(hash.slice(1)); } catch (e) { id = hash.slice(1); }
      var el = document.getElementById(id);
      if (!el) return;
      var n = el;
      while (n && n !== document.body) {
        if (n.tagName === "DETAILS" && !n.open) n.open = true;
        n = n.parentElement;
      }
      if (desplazar && typeof el.scrollIntoView === "function") el.scrollIntoView();
    } catch (e) { /* un ancla rara no rompe la página */ }
  }

  abrirAncla(window.location.hash, true);
  window.addEventListener("hashchange", function () { abrirAncla(window.location.hash, true); });
  document.addEventListener("click", function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href^="#"]') : null;
    // Sin desplazar: la navegación por defecto del ancla desplaza después, ya con el <details> abierto.
    if (a) abrirAncla(a.getAttribute("href"), false);
  });

  // ── Arrastrar y soltar ──────────────────────────────────────────────────────
  var tablero = document.getElementById("tablero");
  if (!tablero || tablero.getAttribute("data-editable") !== "1") return;

  var aviso = document.getElementById("aviso-dnd");
  var dlg = document.getElementById("dlg-mover");

  function avisar(txt) { if (aviso) aviso.textContent = txt; }

  function columnas() { return Array.prototype.slice.call(tablero.querySelectorAll(".col-tablero")); }

  function tituloColumna(col) {
    if (!col) return "";
    var t = col.querySelector("h3 span") || col.querySelector("h3, h2");
    var txt = t ? t.textContent.trim() : "";
    return txt || col.getAttribute("data-columna") || "";
  }

  function formEstado(tarjeta) {
    var forms = tarjeta.querySelectorAll("form.cambio");
    for (var i = 0; i < forms.length; i++) {
      if (forms[i].querySelector('select[name="estado"]')) return forms[i];
    }
    return null;
  }

  function campo(form, nombre) { return form.querySelector('[name="' + nombre + '"]'); }

  var arrastre = null;   // {tarjeta, gid, estado, siguientes, exige, origen, sobre, soltado}
  var pendiente = null;  // {tarjeta, form, estado, destinos, exige, columna} mientras el diálogo está abierto

  function limpiar() {
    if (arrastre && arrastre.tarjeta) arrastre.tarjeta.classList.remove("arrastrando");
    columnas().forEach(function (c) { c.classList.remove("col-valida", "col-invalida", "col-sobre"); });
    arrastre = null;
  }

  function destinosEn(col) {
    if (!arrastre || !col || col === arrastre.origen) return [];
    var estados = csv(col.getAttribute("data-estados"));
    return arrastre.siguientes.filter(function (e) { return estados.indexOf(e) !== -1; });
  }

  function exigeMotivo(estadoActual, destino, exige) {
    return destino === "DESCARTADO" || FINALES[estadoActual] === true || (exige && exige[destino] === true);
  }

  function avisoInvalido(col) {
    if (!arrastre) return;
    var de = tituloColumna(arrastre.origen) || nombreEstado(arrastre.estado);
    if (col === arrastre.origen) {
      avisar("La oportunidad ya está en " + de + "; para corregir su estado usa «Mover a…».");
    } else {
      avisar("No se puede pasar de " + de + " a " + tituloColumna(col) + ".");
    }
  }

  function enviar(form) {
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  }

  // Rellena el formulario de la tarjeta y lo envía; devuelve false si no se pudo.
  function rellenarYEnviar(tarjeta, form, destino, motivo, monto, competidor) {
    var fs = form.querySelector("fieldset");
    if (fs && fs.disabled) { avisar("Esta vista es de solo lectura."); return false; }
    var sel = campo(form, "estado");
    if (!sel) return false;
    sel.value = destino;
    if (sel.value !== destino) {
      avisar("No se puede mover a " + nombreEstado(destino) + "; usa «Mover a…».");
      return false;
    }
    var pares = [["motivo", motivo], ["monto_ofertado", monto], ["competidor", competidor]];
    pares.forEach(function (p) { var c = campo(form, p[0]); if (c) c.value = p[1] || ""; });
    var ref = tarjeta.querySelector(".tk-ref");
    avisar("Moviendo " + (ref ? ref.textContent.trim() : "la oportunidad") + " a " + nombreEstado(destino) + "…");
    enviar(form);
    return true;
  }

  tablero.addEventListener("dragstart", function (ev) {
    try {
      var t = ev.target;
      if (!t || !t.closest || t.closest("form, input, select, textarea")) return;
      var tarjeta = t.closest('.tarjeta-op[draggable="true"]');
      if (!tarjeta || !tablero.contains(tarjeta)) return;
      var gid = tarjeta.getAttribute("data-gid");
      var estado = tarjeta.getAttribute("data-estado");
      var siguientes = csv(tarjeta.getAttribute("data-siguientes"));
      var exige = JSON.parse(tarjeta.getAttribute("data-exige-motivo") || "{}");
      var origen = tarjeta.closest(".col-tablero");
      if (!gid || !estado || !siguientes.length || !exige || typeof exige !== "object" || Array.isArray(exige)
          || !origen || !formEstado(tarjeta)) {
        ev.preventDefault();
        return;
      }
      limpiar();
      arrastre = { tarjeta: tarjeta, gid: gid, estado: estado, siguientes: siguientes, exige: exige,
                   origen: origen, sobre: null, soltado: false };
      if (ev.dataTransfer) {
        try { ev.dataTransfer.setData("text/plain", gid); } catch (e) { /* algunos navegadores */ }
        ev.dataTransfer.effectAllowed = "move";
      }
      tarjeta.classList.add("arrastrando");
      var validas = [];
      columnas().forEach(function (c) {
        if (destinosEn(c).length) { c.classList.add("col-valida"); validas.push(tituloColumna(c)); }
        else c.classList.add("col-invalida");
      });
      var ref = tarjeta.querySelector(".tk-ref");
      avisar("Arrastrando " + (ref ? ref.textContent.trim() : "la oportunidad") + ". Puede ir a: "
             + (validas.length ? validas.join(", ") : "ninguna columna") + ".");
    } catch (e) {
      // data-exige-motivo corrupto u otro dato inválido: no se arrastra.
      try { ev.preventDefault(); } catch (e2) { /* nada */ }
      limpiar();
    }
  });

  function alPasar(ev) {
    if (!arrastre) return;
    var col = ev.target && ev.target.closest ? ev.target.closest(".col-tablero") : null;
    if (!col || !tablero.contains(col)) return;
    arrastre.sobre = col;
    columnas().forEach(function (c) { if (c !== col) c.classList.remove("col-sobre"); });
    if (destinosEn(col).length) {
      ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
      col.classList.add("col-sobre");
    } else if (ev.dataTransfer) {
      ev.dataTransfer.dropEffect = "none";
    }
  }
  tablero.addEventListener("dragenter", alPasar);
  tablero.addEventListener("dragover", alPasar);

  tablero.addEventListener("dragleave", function (ev) {
    var col = ev.target && ev.target.closest ? ev.target.closest(".col-tablero") : null;
    if (col && (!ev.relatedTarget || !col.contains(ev.relatedTarget))) col.classList.remove("col-sobre");
  });

  tablero.addEventListener("drop", function (ev) {
    try {
      if (!arrastre) return;
      ev.preventDefault();
      arrastre.soltado = true;
      var col = ev.target && ev.target.closest ? ev.target.closest(".col-tablero") : null;
      var a = arrastre;
      var destinos = destinosEn(col);
      if (!col || !destinos.length) {
        if (col) avisoInvalido(col);
        limpiar();
        return;
      }
      var form = formEstado(a.tarjeta);
      limpiar();
      if (!form) return;
      if (destinos.length === 1 && !exigeMotivo(a.estado, destinos[0], a.exige)) {
        rellenarYEnviar(a.tarjeta, form, destinos[0], "", "", "");
      } else if (!dlg) {
        avisar("Este cambio necesita más datos; usa «Mover a…» en la tarjeta.");
      } else {
        abrirDialogo({ tarjeta: a.tarjeta, form: form, estado: a.estado, destinos: destinos,
                       exige: a.exige, columna: col });
      }
    } catch (e) {
      limpiar();
    }
  });

  tablero.addEventListener("dragend", function () {
    // Soltar fuera de una columna válida no dispara `drop`: el aviso sale aquí.
    if (arrastre && !arrastre.soltado && arrastre.sobre && !destinosEn(arrastre.sobre).length) {
      avisoInvalido(arrastre.sobre);
    }
    limpiar();
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && arrastre) { limpiar(); avisar("Arrastre cancelado."); }
  });

  // ── Diálogo ─────────────────────────────────────────────────────────────────
  if (!dlg) return;
  var el = function (id) { return document.getElementById(id); };
  var grupo = el("dlg-estados");
  var motivo = el("dlg-motivo");
  var monto = el("dlg-monto");
  var competidor = el("dlg-competidor");
  var error = el("dlg-error");
  var nota = el("dlg-motivo-nota");

  function mostrar(input, contenedorId, visible) {
    var c = el(contenedorId) || (input && input.closest ? input.closest("label") : null);
    if (c) c.hidden = !visible;
  }

  function destinoElegido() {
    if (!grupo) return pendiente ? pendiente.destinos[0] : null;
    var r = grupo.querySelector('input[name="dlg_estado"]:checked');
    return r ? r.value : null;
  }

  function actualizarCampos() {
    if (!pendiente) return;
    var d = destinoElegido();
    if (nota) nota.hidden = !exigeMotivo(pendiente.estado, d, pendiente.exige);
    if (motivo) motivo.required = exigeMotivo(pendiente.estado, d, pendiente.exige);
    mostrar(monto, "dlg-campo-monto", CON_MONTO[d] === true);
    mostrar(competidor, "dlg-campo-competidor", CON_COMPETIDOR[d] === true);
    if (error) error.textContent = "";
  }

  function cerrarDialogo() {
    if (typeof dlg.close === "function") { if (dlg.open) dlg.close(); }
    else dlg.removeAttribute("open");
    pendiente = null;
  }

  function abrirDialogo(p) {
    pendiente = p;
    var ref = p.tarjeta.querySelector(".tk-ref");
    var tit = p.tarjeta.querySelector(".tk-t");
    var h = el("dlg-titulo");
    if (h) h.textContent = "Mover a " + tituloColumna(p.columna);
    var res = el("dlg-resumen");
    if (res) {
      res.textContent = (ref ? ref.textContent.trim() : "") + (ref && tit ? " · " : "")
        + (tit ? tit.textContent.trim() : "") + " — ahora: " + nombreEstado(p.estado);
    }
    if (grupo) {
      Array.prototype.slice.call(grupo.querySelectorAll(".dlg-opcion")).forEach(function (n) { n.remove(); });
      p.destinos.forEach(function (d, i) {
        var lab = document.createElement("label");
        lab.className = "dlg-opcion";
        var r = document.createElement("input");
        r.type = "radio";
        r.name = "dlg_estado";
        r.value = d;
        if (i === 0) r.checked = true;
        r.addEventListener("change", actualizarCampos);
        lab.appendChild(r);
        lab.appendChild(document.createTextNode(" " + nombreEstado(d)));
        grupo.appendChild(lab);
      });
    }
    [motivo, monto, competidor].forEach(function (c) { if (c) c.value = ""; });
    actualizarCampos();
    try {
      if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); }
      else dlg.setAttribute("open", "");
    } catch (e) {
      dlg.setAttribute("open", "");
    }
    var foco = grupo ? grupo.querySelector('input[name="dlg_estado"]:checked') : null;
    if (foco && typeof foco.focus === "function") foco.focus();
  }

  var bCancelar = el("dlg-cancelar");
  if (bCancelar) bCancelar.addEventListener("click", function () { cerrarDialogo(); avisar("Cambio cancelado."); });
  dlg.addEventListener("close", function () { pendiente = null; });

  var bGuardar = el("dlg-guardar");
  if (bGuardar) bGuardar.addEventListener("click", function () {
    try {
      if (!pendiente) { cerrarDialogo(); return; }
      var d = destinoElegido();
      if (!d || pendiente.destinos.indexOf(d) === -1) {
        if (error) error.textContent = "Elige el nuevo estado.";
        return;
      }
      var m = motivo ? motivo.value.trim() : "";
      if (exigeMotivo(pendiente.estado, d, pendiente.exige) && !m) {
        if (error) error.textContent = "El motivo es obligatorio para pasar a " + nombreEstado(d) + ".";
        if (motivo && typeof motivo.focus === "function") motivo.focus();
        return;
      }
      var mo = CON_MONTO[d] && monto ? monto.value.trim() : "";
      var co = CON_COMPETIDOR[d] && competidor ? competidor.value.trim() : "";
      var p = pendiente;
      cerrarDialogo();
      rellenarYEnviar(p.tarjeta, p.form, d, m, mo, co);
    } catch (e) {
      if (error) error.textContent = "No se pudo preparar el cambio; usa «Mover a…».";
    }
  });
})();
