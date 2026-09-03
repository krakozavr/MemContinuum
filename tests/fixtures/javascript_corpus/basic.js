function plain(a, b) {
  return a + b;
}

const arrowed = (x) => x * 2;

export default function DefaultNamed() {
  return null;
}

class Widget {
  constructor(x) {
    this.x = x;
  }

  get value() {
    return this.x;
  }

  set value(v) {
    this.x = v;
  }

  render() {
    return this.x;
  }
}
