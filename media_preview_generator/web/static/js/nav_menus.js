/* Navbar hover menus: Automation and Settings (base.html, `.nav-hover-menu`).
 *
 * At the navbar's expanded width (xl, 1200px and up) the word is a plain link to its page, and its list of the
 * page's sections opens on hover, or from the keyboard with ArrowDown / ArrowUp / Space. Escape closes it.
 * Below xl the same lists sit in the offcanvas phone menu, where a tap on the word expands them like the other
 * groups there.
 *
 * The lists use Bootstrap's dropdown markup and CSS, but this file opens and closes them rather than Bootstrap's
 * Dropdown: its show() focuses the toggle, so a hover would pull focus out of a field the user is typing in (and
 * autosave commits text fields on blur). The toggles carry no data-bs-toggle, so Bootstrap's own click handler
 * leaves them alone. Its `.dropdown-menu` key handler doesn't: it listens on the document in the capture phase and
 * throws on these lists, as it can't find their toggle, so keys in a list are handled one step earlier, on the window.
 */
(function () {
    'use strict';

    var OPEN_DELAY_MS = 100;
    // Time for the pointer to cross the gap between the word and its list, or clip the next item on a diagonal.
    var CLOSE_DELAY_MS = 250;
    var LANDING_HOLD_MS = 10000;
    var expandedNavbar = window.matchMedia('(min-width: 1200px)');
    var menus = [];

    // One navbar list open at a time, Bootstrap's click-opened ones (Tools, Help, notifications) included.
    function closeAll(except) {
        menus.forEach(function (menu) {
            if (menu !== except) menu.close();
        });
        document.querySelectorAll('.navbar [data-bs-toggle="dropdown"].show').forEach(function (toggle) {
            var dropdown = window.bootstrap && window.bootstrap.Dropdown.getInstance(toggle);
            if (dropdown) dropdown.hide();
        });
    }

    function setUpMenu(item) {
        var toggle = item.querySelector('[data-nav-menu-toggle]');
        var list = item.querySelector('.dropdown-menu');
        if (!toggle || !list) return;
        var timer = null;
        var menu = { item: item, list: list };

        function isOpen() {
            return list.classList.contains('show');
        }
        function links() {
            return Array.prototype.slice.call(list.querySelectorAll('.dropdown-item'));
        }
        function focusLink(index) {
            var all = links();
            if (all.length) all[(index + all.length) % all.length].focus();
        }
        function open() {
            clearTimeout(timer);
            closeAll(menu);
            toggle.classList.add('show');
            list.classList.add('show');
            toggle.setAttribute('aria-expanded', 'true');
        }
        function close() {
            clearTimeout(timer);
            toggle.classList.remove('show');
            list.classList.remove('show');
            toggle.setAttribute('aria-expanded', 'false');
        }
        function after(delay, action) {
            clearTimeout(timer);
            timer = setTimeout(action, delay);
        }
        menu.close = close;
        menus.push(menu);

        item.addEventListener('mouseenter', function () {
            if (!expandedNavbar.matches) return;
            if (isOpen()) clearTimeout(timer);
            else after(OPEN_DELAY_MS, open);
        });
        item.addEventListener('mouseleave', function () {
            if (!expandedNavbar.matches) return;
            if (isOpen()) after(CLOSE_DELAY_MS, close);
            else clearTimeout(timer);
        });

        toggle.addEventListener('click', function (event) {
            if (expandedNavbar.matches) return;
            event.preventDefault();
            if (isOpen()) close();
            else open();
        });

        toggle.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown' || event.key === ' ') {
                open();
                focusLink(0);
            } else if (event.key === 'ArrowUp') {
                open();
                focusLink(-1);
            } else if (event.key === 'Escape' && isOpen()) {
                close();
            } else {
                return;
            }
            event.preventDefault();
            event.stopPropagation();
        });

        menu.onListKey = function (event) {
            var index = links().indexOf(document.activeElement);
            if (event.key === 'ArrowDown') {
                focusLink(index + 1);
            } else if (event.key === 'ArrowUp') {
                focusLink(index < 0 ? -1 : index - 1);
            } else if (event.key === 'Home') {
                focusLink(0);
            } else if (event.key === 'End') {
                focusLink(-1);
            } else if (event.key === 'Escape') {
                close();
                toggle.focus();
            } else {
                return;
            }
            event.preventDefault();
            event.stopPropagation();
        };

        // A same-page #section link doesn't reload, so close the list once it's used.
        list.addEventListener('click', function (event) {
            if (event.target.closest('.dropdown-item')) close();
        });

        // Tabbing out closes the list. Left to the click handler below: focus going nowhere (Safari doesn't focus a
        // clicked link, so closing here would hide the list before the click lands), and the phone menu, where the
        // lists are in the flow and closing one on mousedown would move the next word out from under the tap.
        item.addEventListener('focusout', function (event) {
            if (expandedNavbar.matches && event.relatedTarget && !item.contains(event.relatedTarget)) close();
        });
    }

    function markCurrentSection() {
        var hash = window.location.hash.replace(/^#/, '');
        document.querySelectorAll('.nav-hover-menu .dropdown-item[data-anchor]').forEach(function (link) {
            var current = !!hash && link.getAttribute('data-anchor') === hash
                && link.pathname === window.location.pathname;
            link.classList.toggle('active', current);
            if (current) link.setAttribute('aria-current', 'location');
            else link.removeAttribute('aria-current');
        });
    }

    // Landing on /page#section-…: the browser jumps to the section before the page has filled in from its API
    // calls, and what loads above the section then pushes it down the page (Settings on a phone is still growing
    // 2.5 s in). Keep it under the navbar while the page grows, until the user scrolls, clicks or types.
    function holdLandingSection() {
        var hash = window.location.hash;
        var section = hash.indexOf('#section-') === 0 && document.getElementById(hash.slice(1));
        if (!section || !window.ResizeObserver) return;
        var userEvents = ['wheel', 'touchstart', 'mousedown', 'keydown'];
        var observer = new ResizeObserver(function () {
            section.scrollIntoView({ behavior: 'instant' });
        });
        function release() {
            observer.disconnect();
            userEvents.forEach(function (type) {
                window.removeEventListener(type, release);
            });
        }
        observer.observe(document.body);
        userEvents.forEach(function (type) {
            window.addEventListener(type, release, { passive: true });
        });
        setTimeout(release, LANDING_HOLD_MS);
    }

    document.querySelectorAll('.nav-hover-menu').forEach(setUpMenu);
    window.addEventListener('keydown', function (event) {
        menus.forEach(function (menu) {
            if (menu.list.contains(event.target)) menu.onListKey(event);
        });
    }, true);
    document.addEventListener('click', function (event) {
        menus.forEach(function (menu) {
            if (!menu.item.contains(event.target)) menu.close();
        });
    });
    // Escape also closes a list the mouse opened while focus is elsewhere on the page.
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
            menus.forEach(function (menu) {
                menu.close();
            });
        }
    });
    expandedNavbar.addEventListener('change', function () {
        closeAll(null);
    });

    markCurrentSection();
    window.addEventListener('hashchange', markCurrentSection);
    holdLandingSection();
})();
