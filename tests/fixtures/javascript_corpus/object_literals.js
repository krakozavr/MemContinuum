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
