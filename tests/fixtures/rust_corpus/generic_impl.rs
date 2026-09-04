struct Widget<T> {
    value: T,
}

impl<T> Widget<T> {
    fn get(&self) -> &T {
        &self.value
    }
}

trait Render {
    fn render(&self) -> u32;
}

impl<T> Render for Widget<T> {
    fn render(&self) -> u32 {
        1
    }
}

struct Handle<'a> {
    name: &'a str,
}

impl<'a> Handle<'a> {
    fn name(&self) -> &'a str {
        self.name
    }
}
