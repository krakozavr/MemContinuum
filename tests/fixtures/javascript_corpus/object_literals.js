const api = {
  get() {
    return 1;
  },
};

let store;
store = {
  get() {
    return 2;
  },
};

register({
  get() {
    return 3;
  },
});

const first = {
  get open() {
    return 4;
  },
};

const second = {
  get open() {
    return 5;
  },
  set open(v) {
    this._v = v;
  },
  constructor() {
    return 6;
  },
};
