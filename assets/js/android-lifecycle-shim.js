(function () {
  function install() {
    if (!window.MiruAndroid) return false;
    window.Capacitor = window.Capacitor || {};
    if (typeof window.Capacitor.triggerEvent === 'function') return true;

    window.Capacitor.triggerEvent = function (eventName, target, eventData) {
      var receiver = target === 'window' ? window : document;
      var event;
      try {
        event = new CustomEvent(eventName, { detail: eventData });
      } catch (e) {
        event = document.createEvent('Event');
        event.initEvent(eventName, false, false);
        event.detail = eventData;
      }
      receiver.dispatchEvent(event);
    };
    return true;
  }

  if (install()) return;
  var attempts = 0;
  var timer = setInterval(function () {
    attempts += 1;
    if (install() || attempts >= 100) clearInterval(timer);
  }, 50);
})();
