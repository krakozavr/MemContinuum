public class ErrorRecovery {
    public int good(int a) {
        return a + 1;
    }

    public int broken(int c) {
        return @@@;
    }

    public int alsoGood(int b) {
        return b * 2;
    }
}
