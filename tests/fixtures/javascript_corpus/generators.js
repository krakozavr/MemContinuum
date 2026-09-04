function* counter() {
  yield 1;
}

async function* asyncCounter() {
  yield 2;
}

const bound = function* () {
  yield 3;
};

export function* exported() {
  yield 4;
}
