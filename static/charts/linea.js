// Hover por punto de la serie: muestra el valor exacto en el tooltip compartido.
document.querySelectorAll("svg.linea, svg.medidor").forEach(function (svg) {
  window.AmaliaTip.atar(svg);
});
