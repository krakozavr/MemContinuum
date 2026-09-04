function outer(a) {
  function inner(b) {
    return b + 1;
  }
  return inner(a);
}

class Widget {
  method() {
    function helper() {
      return 1;
    }
    return helper();
  }
}
