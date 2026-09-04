fn outer(a: i32) -> i32 {
    fn inner(b: i32) -> i32 {
        b + 1
    }
    inner(a)
}

struct Widget;

impl Widget {
    fn method(&self) -> i32 {
        fn helper() -> i32 {
            1
        }
        helper()
    }
}
