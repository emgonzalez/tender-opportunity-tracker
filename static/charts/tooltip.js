// Tooltip compartido: cualquier elemento con [data-tip] muestra su texto al pasar el
// cursor o recibir foco. Sin dependencias; si el JS no corre, el <title> del SVG queda.
(function () {
  var tip = document.createElement("div");
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  window.AmaliaTip = {
    atar: function (raiz) {
      raiz.querySelectorAll("[data-tip]").forEach(function (el) {
        el.addEventListener("mousemove", function (e) {
          tip.textContent = el.getAttribute("data-tip");
          tip.hidden = false;
          tip.style.left = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8) + "px";
          tip.style.top = e.clientY + 16 + "px";
        });
        el.addEventListener("mouseleave", function () { tip.hidden = true; });
      });
    },
  };
})();
