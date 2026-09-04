function outer(a: number): number {
  function inner(b: number): number {
    return b + 1;
  }
  return inner(a);
}

class Widget {
  method(): number {
    function helper(): number {
      return 1;
    }
    return helper();
  }
}
