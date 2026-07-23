#include <iostream>
#include <string>
#include <sstream>
#include <iomanip>
#include <cstring>
#include <limits>
#include <regex>
#include <cctype>
#include <cstdlib>



/**
 * Allocate and return a heap-owned C string copy of the given std::string.
 *
 * Memory ownership:
 * - The caller / host runtime is responsible for eventually freeing this memory
 *   if required by the embedding environment.
 */
static char* copy_to_cstr(const std::string& s) {
    char* result = (char*)std::malloc(s.size() + 1);
    if (!result) return nullptr;
    std::strcpy(result, s.c_str());
    return result;
}

/**
 * Safely convert a nullable C string to std::string.
 * - nullptr is treated as the empty string.
 */
static std::string to_string_safe(const char* input) {
    return input == nullptr ? std::string() : std::string(input);
}

/**
 * Return true iff s starts with prefix.
 */
static bool starts_with_impl(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.rfind(prefix, 0) == 0;
}

/**
 * Return true iff s ends with suffix.
 */
static bool ends_with_impl(const std::string& s, const std::string& suffix) {
    return s.size() >= suffix.size() &&
           s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

/**
 * Return true iff c is a hexadecimal digit.
 */
static bool is_hex_digit(char c) {
    return std::isxdigit(static_cast<unsigned char>(c)) != 0;
}
// Extern "C" block to allow linkage with C code
extern "C" {

// Function to convert a C string (char*) to an IRI
char* toIRI(const char* input) {
    // Convert the input C string to a C++ string
    std::string str(input);
    std::ostringstream iriStream;

    for (char c : str) {
        // Percent-encode characters that are not allowed in IRIs
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            iriStream << c;
        } else {
            // Convert the character to a percent-encoded string
            iriStream << '%' << std::uppercase << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(static_cast<unsigned char>(c));
        }
    }

    // Convert the result back to a C string (char*)
    std::string iriStr = iriStream.str();
    char* result = (char*)malloc(iriStr.length() + 1); // Allocate memory for the C string
    std::strcpy(result, iriStr.c_str()); // Copy the C++ string to the allocated C string

    return result;
}

char* toDoubleLiteral(const char* input) {
    if (!input) return NULL;

    // Convert input to a double
    double number;
    std::istringstream iss(input);
    iss >> number;
    if (iss.fail()) return NULL;

    // Create an output string stream
    std::ostringstream oss;

    // Format the number in scientific notation with uppercase
    oss << std::uppercase << std::scientific << number;

    // Get the formatted string
    std::string result = oss.str();

    // Find the position of the exponent ('E')
    size_t ePos = result.find('E');
    if (ePos != std::string::npos) {
        // Remove unnecessary trailing zeros in the mantissa part
        size_t dotPos = result.find('.');
        if (dotPos != std::string::npos && dotPos < ePos) {
            size_t lastNonZero = result.find_last_not_of('0', ePos - 1);
            if (lastNonZero != std::string::npos && lastNonZero > dotPos) {
                result.erase(lastNonZero + 1, ePos - lastNonZero - 1); // Remove trailing zeros
                ePos = result.find('E'); // Recalculate ePos after modification
            }
            if (dotPos < result.size() && result[dotPos + 1] == 'E') {
                result.erase(dotPos, 1); // Remove the dot if no decimals remain
                ePos = result.find('E'); // Recalculate ePos after modification
            }
        }

        // Clean up the exponent part
        if (ePos + 1 < result.size() && result[ePos + 1] == '+') {
            result.erase(ePos + 1, 1); // Remove '+' sign
            ePos = result.find('E'); // Recalculate ePos after modification
        }
        if (ePos + 1 < result.size() && result[ePos + 1] == '0' && ePos + 2 < result.size() && isdigit(result[ePos + 2])) {
            result.erase(ePos + 1, 1); // Remove leading zero in exponent
        }
    }

    // Allocate memory for the C string
    char* cstr = (char*)malloc(result.length() + 1);
    if (!cstr) return NULL;
    std::strcpy(cstr, result.c_str());

    return cstr;
}

char* convertDateTime(const char* input) {
    if (!input) return NULL;

    // Convert input to a C++ string for easier manipulation
    std::string dateTime(input);

    // Check if the input format is as expected: "YYYY-MM-DD HH:MM:SS"
    size_t spacePos = dateTime.find(' ');
    if (spacePos == std::string::npos || spacePos != 10 || dateTime.length() != 19) {
        return NULL; // Invalid format
    }

    // Replace the space with 'T'
    dateTime[spacePos] = 'T';

    // Allocate memory for the output C string
    char* result = (char*)malloc(dateTime.length() + 1);
    if (!result) return NULL;

    // Copy the modified string into the allocated memory
    std::strcpy(result, dateTime.c_str());

    return result;
}

extern "C" char* convertBool(const char* input) {
    // Check for null input
    if (!input) return NULL;

    // Convert input to a C++ string for easier manipulation
    std::string strInput(input);

    // Check for "true" cases
    if (strInput == "t" || strInput == "true" || strInput == "TRUE" || strInput == "1") {
        const char* trueStr = "true";
        char* result = (char*)malloc(strlen(trueStr) + 1);
        if (!result) return NULL;
        std::strcpy(result, trueStr);
        return result;
    }

    // Default case: "false"
    const char* falseStr = "false";
    char* result = (char*)malloc(strlen(falseStr) + 1);
    if (!result) return NULL;
    std::strcpy(result, falseStr);
    return result;
}


char* extract_second_iri(const char* input) {
    std::string iri(input);

    // Find the second occurrence of "http"
    size_t first = iri.find("http");
    size_t second = iri.find("http", first + 1);

    std::string result;

    if (second != std::string::npos) {
        result = "<"+iri.substr(second);
    } else {
        result = iri; // Return as-is if no second IRI
    }

    // Allocate and copy result
    char* output = (char*) std::malloc(result.size() + 1);
    std::strcpy(output, result.c_str());
    return output;
}

char* clean_value(const char* input) {
    if (!input || std::strlen(input) == 0)
        return nullptr;

    std::string val(input);

    // Check for exactly ""
    if (val == "\"\"")
        return nullptr;

    // Lowercase copy to check for "null"
    std::string lower;
    lower.reserve(val.size());
    for (char c : val)
        lower += std::tolower(static_cast<unsigned char>(c));

    if (lower.find("null") != std::string::npos)
        return nullptr;

    // Allocate a copy to return
    char* result = new char[val.size() + 1];
    std::strcpy(result, val.c_str());
    return result;
}


/* ============================================================================
 * Boolean-style helper functions
 *
 * These return:
 * - "1" for true
 * - "0" for false
 *
 * This is often easier to work with in Soufflé than raw C++ bool.
 * ========================================================================== */

/**
 * startsWith(input, prefix)
 *
 * Returns:
 * - "1" if input starts with prefix
 * - "0" otherwise
 *
 * Examples:
 * - startsWith("abc", "a") -> "1"
 * - startsWith("abc", "b") -> "0"
 */
char* startsWith(const char* input, const char* prefix) {
    const std::string s = to_string_safe(input);
    const std::string p = to_string_safe(prefix);
    return copy_to_cstr(starts_with_impl(s, p) ? "1" : "0");
}

/**
 * endsWith(input, suffix)
 *
 * Returns:
 * - "1" if input ends with suffix
 * - "0" otherwise
 *
 * Examples:
 * - endsWith("abc", "bc") -> "1"
 * - endsWith("abc", "ab") -> "0"
 */
char* endsWith(const char* input, const char* suffix) {
    const std::string s = to_string_safe(input);
    const std::string suf = to_string_safe(suffix);
    return copy_to_cstr(ends_with_impl(s, suf) ? "1" : "0");
}

/**
 * containsStr(input, needle)
 *
 * Returns:
 * - "1" if needle occurs anywhere in input
 * - "0" otherwise
 *
 * Examples:
 * - containsStr("abc", "b") -> "1"
 * - containsStr("abc", "z") -> "0"
 */
char* containsStr(const char* input, const char* needle) {
    const std::string s = to_string_safe(input);
    const std::string n = to_string_safe(needle);
    return copy_to_cstr(s.find(n) != std::string::npos ? "1" : "0");
}

/**
 * isAngleBracketed(input)
 *
 * Returns:
 * - "1" if input has the form <...>
 * - "0" otherwise
 *
 * Examples:
 * - isAngleBracketed("<http://x>") -> "1"
 * - isAngleBracketed("http://x") -> "0"
 */
char* isAngleBracketed(const char* input) {
    const std::string s = to_string_safe(input);
    return copy_to_cstr((s.size() >= 2 && s.front() == '<' && s.back() == '>') ? "1" : "0");
}

/**
 * isQuotedLiteral(input)
 *
 * Returns:
 * - "1" if input has the form "..."
 * - "0" otherwise
 *
 * Examples:
 * - isQuotedLiteral("\"Alice\"") -> "1"
 * - isQuotedLiteral("Alice") -> "0"
 */
char* isQuotedLiteral(const char* input) {
    const std::string s = to_string_safe(input);
    return copy_to_cstr((s.size() >= 2 && s.front() == '"' && s.back() == '"') ? "1" : "0");
}

/**
 * isTypedLiteral(input, datatype)
 *
 * Returns:
 * - "1" if input has the form "lex"^^<datatype>
 * - "0" otherwise
 *
 * Examples:
 * - isTypedLiteral("\"1\"^^<http://www.w3.org/2001/XMLSchema#integer>", "http://www.w3.org/2001/XMLSchema#integer") -> "1"
 * - isTypedLiteral("\"1\"", "http://www.w3.org/2001/XMLSchema#integer") -> "0"
 */
char* isTypedLiteral(const char* input, const char* datatype) {
    const std::string s = to_string_safe(input);
    const std::string dt = to_string_safe(datatype);
    const std::string suffix = "^^<" + dt + ">";

    if (s.size() < suffix.size() + 2) return copy_to_cstr("0");
    if (!ends_with_impl(s, suffix)) return copy_to_cstr("0");

    std::string lit = s.substr(0, s.size() - suffix.size());
    return copy_to_cstr((lit.size() >= 2 && lit.front() == '"' && lit.back() == '"') ? "1" : "0");
}

/**
 * isLanguageLiteral(input)
 *
 * Returns:
 * - "1" if input has the form "lex"@lang
 * - "0" otherwise
 *
 * Notes:
 * - This is a lightweight structural check, not a full RFC validation.
 *
 * Examples:
 * - isLanguageLiteral("\"hello\"@en") -> "1"
 * - isLanguageLiteral("\"hello\"") -> "0"
 */
char* isLanguageLiteral(const char* input) {
    const std::string s = to_string_safe(input);
    auto at = s.rfind('@');
    if (at == std::string::npos || at == 0 || at + 1 >= s.size()) return copy_to_cstr("0");
    std::string lit = s.substr(0, at);
    if (!(lit.size() >= 2 && lit.front() == '"' && lit.back() == '"')) return copy_to_cstr("0");
    return copy_to_cstr("1");
}

/* ============================================================================
 * String transformation helpers
 *
 * Convention:
 * - On successful transformation: return the transformed string
 * - On structural mismatch / failure: return the empty string ""
 *
 * This is useful in reverse reconstruction pipelines, where failure should not
 * silently invent values.
 * ========================================================================== */

/**
 * decodeIRI(input)
 *
 * Percent-decodes IRI-escaped sequences such as:
 * - "%20" -> space
 * - "%2F" -> '/'
 *
 * Any non-percent-encoded characters are copied unchanged.
 *
 * Examples:
 * - decodeIRI("Alice%20Bob") -> "Alice Bob"
 * - decodeIRI("a%2Fb") -> "a/b"
 */
char* decodeIRI(const char* input) {
    const std::string s = to_string_safe(input);
    std::ostringstream out;

    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '%' && i + 2 < s.size() && is_hex_digit(s[i + 1]) && is_hex_digit(s[i + 2])) {
            std::string hex = s.substr(i + 1, 2);
            char decoded = static_cast<char>(std::strtol(hex.c_str(), nullptr, 16));
            out << decoded;
            i += 2;
        } else {
            out << s[i];
        }
    }

    return copy_to_cstr(out.str());
}

/**
 * stripAngleBrackets(input)
 *
 * Removes outer angle brackets from an IRI-like string.
 *
 * Returns:
 * - inner content if input is of the form <...>
 * - "" on mismatch
 *
 * Examples:
 * - stripAngleBrackets("<http://example.com/x>") -> "http://example.com/x"
 * - stripAngleBrackets("http://example.com/x") -> ""
 */
char* stripAngleBrackets(const char* input) {
    const std::string s = to_string_safe(input);
    if (s.size() >= 2 && s.front() == '<' && s.back() == '>') {
        return copy_to_cstr(s.substr(1, s.size() - 2));
    }
    return copy_to_cstr("");
}

/**
 * addAngleBrackets(input)
 *
 * Wraps the input as <input>.
 *
 * Examples:
 * - addAngleBrackets("http://example.com/x") -> "<http://example.com/x>"
 */
char* addAngleBrackets(const char* input) {
    const std::string s = to_string_safe(input);
    return copy_to_cstr("<" + s + ">");
}

/**
 * removePrefix(input, prefix)
 *
 * Removes prefix from input if present.
 *
 * Returns:
 * - input without prefix if input starts with prefix
 * - "" otherwise
 *
 * Examples:
 * - removePrefix("abc", "a") -> "bc"
 * - removePrefix("abc", "z") -> ""
 */
char* removePrefix(const char* input, const char* prefix) {
    const std::string s = to_string_safe(input);
    const std::string p = to_string_safe(prefix);
    if (starts_with_impl(s, p)) {
        return copy_to_cstr(s.substr(p.size()));
    }
    return copy_to_cstr("");
}

/**
 * removeSuffix(input, suffix)
 *
 * Removes suffix from input if present.
 *
 * Returns:
 * - input without suffix if input ends with suffix
 * - "" otherwise
 *
 * Examples:
 * - removeSuffix("abc", "bc") -> "a"
 * - removeSuffix("abc", "ab") -> ""
 */
char* removeSuffix(const char* input, const char* suffix) {
    const std::string s = to_string_safe(input);
    const std::string suf = to_string_safe(suffix);
    if (ends_with_impl(s, suf)) {
        return copy_to_cstr(s.substr(0, s.size() - suf.size()));
    }
    return copy_to_cstr("");
}

/**
 * beforeFirst(input, delim)
 *
 * Returns substring before the first occurrence of delim.
 *
 * Returns:
 * - substring before delim
 * - "" if delim is absent or empty
 *
 * Examples:
 * - beforeFirst("a/b/c", "/") -> "a"
 * - beforeFirst("abc", "/") -> ""
 */
char* beforeFirst(const char* input, const char* delim) {
    const std::string s = to_string_safe(input);
    const std::string d = to_string_safe(delim);
    if (d.empty()) return copy_to_cstr("");
    size_t pos = s.find(d);
    if (pos == std::string::npos) return copy_to_cstr("");
    return copy_to_cstr(s.substr(0, pos));
}

/**
 * afterFirst(input, delim)
 *
 * Returns substring after the first occurrence of delim.
 *
 * Returns:
 * - substring after delim
 * - "" if delim is absent or empty
 *
 * Examples:
 * - afterFirst("a/b/c", "/") -> "b/c"
 * - afterFirst("abc", "/") -> ""
 */
char* afterFirst(const char* input, const char* delim) {
    const std::string s = to_string_safe(input);
    const std::string d = to_string_safe(delim);
    if (d.empty()) return copy_to_cstr("");
    size_t pos = s.find(d);
    if (pos == std::string::npos) return copy_to_cstr("");
    return copy_to_cstr(s.substr(pos + d.size()));
}

/**
 * beforeLast(input, delim)
 *
 * Returns substring before the last occurrence of delim.
 *
 * Returns:
 * - substring before the last delim
 * - "" if delim is absent or empty
 *
 * Examples:
 * - beforeLast("a/b/c", "/") -> "a/b"
 * - beforeLast("abc", "/") -> ""
 */
char* beforeLast(const char* input, const char* delim) {
    const std::string s = to_string_safe(input);
    const std::string d = to_string_safe(delim);
    if (d.empty()) return copy_to_cstr("");
    size_t pos = s.rfind(d);
    if (pos == std::string::npos) return copy_to_cstr("");
    return copy_to_cstr(s.substr(0, pos));
}

/**
 * afterLast(input, delim)
 *
 * Returns substring after the last occurrence of delim.
 *
 * Returns:
 * - substring after the last delim
 * - "" if delim is absent or empty
 *
 * Examples:
 * - afterLast("a/b/c", "/") -> "c"
 * - afterLast("abc", "/") -> ""
 */
char* afterLast(const char* input, const char* delim) {
    const std::string s = to_string_safe(input);
    const std::string d = to_string_safe(delim);
    if (d.empty()) return copy_to_cstr("");
    size_t pos = s.rfind(d);
    if (pos == std::string::npos) return copy_to_cstr("");
    return copy_to_cstr(s.substr(pos + d.size()));
}

/**
 * stripLiteralQuotes(input)
 *
 * Removes outer double quotes from a plain literal.
 *
 * Returns:
 * - inner lexical form if input is of the form "..."
 * - "" otherwise
 *
 * Examples:
 * - stripLiteralQuotes("\"Alice\"") -> "Alice"
 * - stripLiteralQuotes("Alice") -> ""
 */
char* stripLiteralQuotes(const char* input) {
    const std::string s = to_string_safe(input);
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"') {
        return copy_to_cstr(s.substr(1, s.size() - 2));
    }
    return copy_to_cstr("");
}

/**
 * addLiteralQuotes(input)
 *
 * Wraps the input as a quoted literal.
 *
 * Examples:
 * - addLiteralQuotes("Alice") -> "\"Alice\""
 */
char* addLiteralQuotes(const char* input) {
    const std::string s = to_string_safe(input);
    return copy_to_cstr("\"" + s + "\"");
}

/**
 * stripTypedLiteral(input, datatype)
 *
 * Removes the outer quotes and datatype suffix from a typed literal.
 *
 * Expected input form:
 * - "lex"^^<datatype>
 *
 * Returns:
 * - lexical form if the structure and datatype match exactly
 * - "" otherwise
 *
 * Examples:
 * - stripTypedLiteral("\"1\"^^<http://www.w3.org/2001/XMLSchema#integer>", "http://www.w3.org/2001/XMLSchema#integer") -> "1"
 * - stripTypedLiteral("\"1\"", "http://www.w3.org/2001/XMLSchema#integer") -> ""
 */
char* stripTypedLiteral(const char* input, const char* datatype) {
    const std::string s = to_string_safe(input);
    const std::string dt = to_string_safe(datatype);
    const std::string suffix = "^^<" + dt + ">";

    if (!ends_with_impl(s, suffix)) return copy_to_cstr("");

    std::string lit = s.substr(0, s.size() - suffix.size());
    if (lit.size() >= 2 && lit.front() == '"' && lit.back() == '"') {
        return copy_to_cstr(lit.substr(1, lit.size() - 2));
    }
    return copy_to_cstr("");
}

/**
 * makeTypedLiteral(lexical, datatype)
 *
 * Constructs a typed literal of the form:
 * - "lexical"^^<datatype>
 *
 * Examples:
 * - makeTypedLiteral("1", "http://www.w3.org/2001/XMLSchema#integer")
 *   -> "\"1\"^^<http://www.w3.org/2001/XMLSchema#integer>"
 */
char* makeTypedLiteral(const char* lexical, const char* datatype) {
    const std::string lex = to_string_safe(lexical);
    const std::string dt = to_string_safe(datatype);
    return copy_to_cstr("\"" + lex + "\"^^<" + dt + ">");
}

/**
 * stripLanguageLiteral(input)
 *
 * Removes outer quotes and language tag from a language-tagged literal.
 *
 * Expected form:
 * - "lex"@lang
 *
 * Returns:
 * - lexical form if structure matches
 * - "" otherwise
 *
 * Examples:
 * - stripLanguageLiteral("\"hello\"@en") -> "hello"
 * - stripLanguageLiteral("\"hello\"") -> ""
 */
char* stripLanguageLiteral(const char* input) {
    const std::string s = to_string_safe(input);
    auto at = s.rfind('@');
    if (at == std::string::npos || at == 0 || at + 1 >= s.size()) return copy_to_cstr("");

    std::string lit = s.substr(0, at);
    if (lit.size() >= 2 && lit.front() == '"' && lit.back() == '"') {
        return copy_to_cstr(lit.substr(1, lit.size() - 2));
    }
    return copy_to_cstr("");
}

/**
 * languageTag(input)
 *
 * Extracts the language tag from a language-tagged literal.
 *
 * Expected form:
 * - "lex"@lang
 *
 * Returns:
 * - the tag if structure matches
 * - "" otherwise
 *
 * Examples:
 * - languageTag("\"hello\"@en") -> "en"
 * - languageTag("\"hello\"") -> ""
 */
char* languageTag(const char* input) {
    const std::string s = to_string_safe(input);
    auto at = s.rfind('@');
    if (at == std::string::npos || at + 1 >= s.size()) return copy_to_cstr("");

    std::string lit = s.substr(0, at);
    if (lit.size() >= 2 && lit.front() == '"' && lit.back() == '"') {
        return copy_to_cstr(s.substr(at + 1));
    }
    return copy_to_cstr("");
}

/**
 * concat2(a, b)
 *
 * Concatenates two strings.
 *
 * Examples:
 * - concat2("ab", "cd") -> "abcd"
 */
char* concat2(const char* a, const char* b) {
    return copy_to_cstr(to_string_safe(a) + to_string_safe(b));
}

/**
 * concat3(a, b, c)
 *
 * Concatenates three strings.
 *
 * Examples:
 * - concat3("a", "b", "c") -> "abc"
 */
char* concat3(const char* a, const char* b, const char* c) {
    return copy_to_cstr(to_string_safe(a) + to_string_safe(b) + to_string_safe(c));
}

/**
 * ifElse(cond, whenTrue, whenFalse)
 *
 * Returns:
 * - whenTrue if cond == "1"
 * - whenFalse otherwise
 *
 * Useful when boolean-style helper functions return "1"/"0".
 *
 * Examples:
 * - ifElse("1", "x", "y") -> "x"
 * - ifElse("0", "x", "y") -> "y"
 */
char* ifElse(const char* cond, const char* whenTrue, const char* whenFalse) {
    const std::string c = to_string_safe(cond);
    return copy_to_cstr(c == "1" ? to_string_safe(whenTrue) : to_string_safe(whenFalse));
}


} // end of extern "C"

