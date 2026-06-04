(function () {
  function closeToast(toast) {
    if (!toast || toast.classList.contains('is-closing')) {
      return;
    }

    toast.classList.add('is-closing');
    window.setTimeout(function () {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 240);
  }

  function bindToast(toast, timeoutMs) {
    var closeButton = toast.querySelector('.flash-close');
    if (closeButton) {
      closeButton.addEventListener('click', function () {
        closeToast(toast);
      });
    }

    window.setTimeout(function () {
      closeToast(toast);
    }, timeoutMs);
  }

  document.addEventListener('DOMContentLoaded', function () {
    var stack = document.querySelector('.flash-stack');
    if (!stack) {
      return;
    }

    var toasts = stack.querySelectorAll('.flash');
    if (!toasts.length) {
      return;
    }

    toasts.forEach(function (toast, index) {
      var timeoutMs = 4200 + (index * 450);
      bindToast(toast, timeoutMs);
    });
  });
})();
