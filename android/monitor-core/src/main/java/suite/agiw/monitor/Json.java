package suite.agiw.monitor;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Minimal JSON reader for observer payloads. It does not repair or invent fields. */
final class Json {
    private final String text;
    private int index;

    private Json(String text) {
        this.text = text == null ? "" : text;
    }

    static Object parse(String text) {
        Json parser = new Json(text);
        parser.skip();
        Object value = parser.value();
        parser.skip();
        if (parser.index != parser.text.length()) throw new IllegalArgumentException("Trailing JSON");
        return value;
    }

    private Object value() {
        if (index >= text.length()) throw new IllegalArgumentException("Truncated JSON");
        char c = text.charAt(index);
        if (c == '{') return object();
        if (c == '[') return array();
        if (c == '"') return string();
        if (c == 't' || c == 'f') return bool();
        if (c == 'n') return nil();
        return number();
    }

    private Map<String, Object> object() {
        expect('{');
        Map<String, Object> map = new LinkedHashMap<>();
        skip();
        if (peek('}')) { index++; return map; }
        while (true) {
            skip();
            String key = string();
            skip();
            expect(':');
            skip();
            map.put(key, value());
            skip();
            if (peek('}')) { index++; return map; }
            expect(',');
        }
    }

    private List<Object> array() {
        expect('[');
        List<Object> list = new ArrayList<>();
        skip();
        if (peek(']')) { index++; return list; }
        while (true) {
            skip();
            list.add(value());
            skip();
            if (peek(']')) { index++; return list; }
            expect(',');
        }
    }

    private String string() {
        expect('"');
        StringBuilder out = new StringBuilder();
        while (index < text.length()) {
            char c = text.charAt(index++);
            if (c == '"') return out.toString();
            if (c != '\\') { out.append(c); continue; }
            if (index >= text.length()) break;
            char esc = text.charAt(index++);
            switch (esc) {
                case '"', '\\', '/' -> out.append(esc);
                case 'b' -> out.append('\b');
                case 'f' -> out.append('\f');
                case 'n' -> out.append('\n');
                case 'r' -> out.append('\r');
                case 't' -> out.append('\t');
                case 'u' -> {
                    if (index + 4 > text.length()) throw new IllegalArgumentException("Bad unicode");
                    out.append((char) Integer.parseInt(text.substring(index, index + 4), 16));
                    index += 4;
                }
                default -> throw new IllegalArgumentException("Bad escape");
            }
        }
        throw new IllegalArgumentException("Unclosed string");
    }

    private Object number() {
        int start = index;
        if (peek('-')) index++;
        while (index < text.length() && Character.isDigit(text.charAt(index))) index++;
        if (peek('.')) {
            index++;
            while (index < text.length() && Character.isDigit(text.charAt(index))) index++;
        }
        if (start == index) throw new IllegalArgumentException("Bad number");
        String token = text.substring(start, index);
        if (token.indexOf('.') >= 0) return Double.valueOf(token);
        return Long.valueOf(token);
    }

    private Boolean bool() {
        if (text.startsWith("true", index)) { index += 4; return Boolean.TRUE; }
        if (text.startsWith("false", index)) { index += 5; return Boolean.FALSE; }
        throw new IllegalArgumentException("Bad boolean");
    }

    private Object nil() {
        if (!text.startsWith("null", index)) throw new IllegalArgumentException("Bad null");
        index += 4;
        return null;
    }

    private void skip() {
        while (index < text.length() && Character.isWhitespace(text.charAt(index))) index++;
    }

    private boolean peek(char c) {
        return index < text.length() && text.charAt(index) == c;
    }

    private void expect(char c) {
        if (!peek(c)) throw new IllegalArgumentException("Expected " + c);
        index++;
    }
}
