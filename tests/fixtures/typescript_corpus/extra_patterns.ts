const boxed = function (x: number): number {
  return x - 1;
};

class Accessors {
  get value(): number {
    return this._v;
  }

  set value(v: number) {
    this._v = v;
  }
}
