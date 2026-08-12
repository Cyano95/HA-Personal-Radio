/**
 * Personal Radio — Custom Panel Element
 * Served from /config/www/personal_radio_panel.js → /local/personal_radio_panel.js
 * Loaded by panel_custom in configuration.yaml.
 */
class PersonalRadioPanel extends HTMLElement {
  connectedCallback() {
    if (this._ready) return;
    this._ready = true;
    Object.assign(this.style, {
      display: 'block', width: '100%', height: '100%', position: 'relative',
    });
    const iframe = document.createElement('iframe');
    iframe.src = '/api/hassio/ingress/personal-radio';
    iframe.allow = 'autoplay';
    Object.assign(iframe.style, {
      position: 'absolute', inset: '0',
      width: '100%', height: '100%', border: 'none',
    });
    this.appendChild(iframe);
  }
  disconnectedCallback() { this._ready = false; }
}
customElements.define('personal-radio-panel', PersonalRadioPanel);
