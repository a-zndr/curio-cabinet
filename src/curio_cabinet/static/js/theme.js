/* Applies the saved theme before first paint. Loaded WITHOUT defer,
   ahead of the stylesheets' first use, to avoid a light/dark flash. */
(function () {
  var saved = localStorage.getItem("cc-theme");
  if (saved) document.documentElement.dataset.theme = saved;
})();
