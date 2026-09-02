"""Fixture: class shapes -- method, constructor, property+setter, nested class."""


class Widget:
    """A widget with a constructor and a plain method."""

    def __init__(self, name):
        """Store the widget's name."""
        self.name = name

    def render(self):
        """Render the widget."""
        return self.name


class Gadget:
    """A gadget exposing a computed property with a setter."""

    def __init__(self):
        self._value = 0

    @property
    def value(self):
        """The gadget's current value."""
        return self._value

    @value.setter
    def value(self, new_value):
        self._value = new_value


class Outer:
    """A class containing a nested class."""

    class Inner:
        """The nested class."""

        def greet(self):
            return "hi"
