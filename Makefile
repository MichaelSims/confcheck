SHELL = /bin/sh

confcheck      = $(DESTDIR)/usr/bin/confcheck.py
confcheck_conf = $(DESTDIR)/etc/confcheck.conf
working_copy_dir = $(DESTDIR)/var/cache/confcheck
confcheck_cron = $(DESTDIR)/etc/cron.d/confcheck
confcheck_user = confcheck

install: confcheck.py confcheck.conf cron.d-confcheck
	install --directory --mode=0775 --owner=$(confcheck_user) $(working_copy_dir)
	cp confcheck.py $(confcheck)
	chown root:root $(confcheck)
	chmod 755 $(confcheck)
	cp confcheck.conf $(confcheck_conf)
	chown root:root $(confcheck_conf)
	chmod 644 $(confcheck_conf)
	cp cron.d-confcheck $(confcheck_cron)
	chown root:root $(confcheck_cron)
	chmod 644 $(confcheck_cron)

uninstall:
	-rm $(confcheck) $(confcheck_conf) $(confcheck_cron)
	-rm -Rf $(working_copy_dir)

.PHONY: install uninstall
