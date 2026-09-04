/// Adds one to the input.
fn top_level(x: i32) -> i32 {
    x + 1
}

struct Widget {
    x: i32,
}

impl Widget {
    fn new(x: i32) -> Widget {
        Widget { x }
    }

    fn value(&self) -> i32 {
        self.x
    }
}

trait Greeter {
    fn greet(&self) -> String {
        String::from("hi")
    }
}

impl Greeter for Widget {
    fn greet(&self) -> String {
        String::from("widget hi")
    }
}

mod util {
    fn helper() -> i32 {
        42
    }
}

trait Named {
    fn name(&self) -> String;
}

/* Adds two numbers. */
fn add_two(a: i32, b: i32) -> i32 {
    a + b
}
