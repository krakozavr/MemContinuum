class Box {
  #secret() {
    return 1;
  }

  get #hidden() {
    return this._v;
  }

  set #hidden(v) {
    this._v = v;
  }

  open() {
    return this.#secret();
  }
}
