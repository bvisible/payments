frappe.provide('frappe.twint');

(function() {
    // Utility function to create a jQuery-like wrapper
    const createJQueryLike = (elements) => {
        return {
            length: elements ? elements.length : 0,
            elements: elements || [],
            addClass: function(className) {
                this.elements.forEach(el => {
                    if (el && el.classList && typeof el.classList.add === 'function') {
                        el.classList.add(className);
                    }
                });
                return this;
            },
            removeClass: function(className) {
                this.elements.forEach(el => {
                    if (el && el.classList && typeof el.classList.remove === 'function') {
                        el.classList.remove(className);
                    }
                });
                return this;
            },
            html: function(content) {
                this.elements.forEach(el => {
                    if (el && typeof el.innerHTML !== 'undefined') {
                        el.innerHTML = content;
                    }
                });
                return this;
            },
            text: function(content) {
                if (content === undefined) {
                    return this.elements[0] && typeof this.elements[0].textContent !== 'undefined' ? this.elements[0].textContent : '';
                }
                this.elements.forEach(el => {
                    if (el && typeof el.textContent !== 'undefined') {
                        el.textContent = content;
                    }
                });
                return this;
            },
            attr: function(name, value) {
                if (value === undefined) {
                    return this.elements[0] && typeof this.elements[0].getAttribute === 'function' ? this.elements[0].getAttribute(name) : null;
                }
                this.elements.forEach(el => {
                    if (el && typeof el.setAttribute === 'function') {
                        el.setAttribute(name, value);
                    }
                });
                return this;
            },
            show: function() {
                this.elements.forEach(el => {
                    if (el && el.style) {
                        el.style.display = '';
                    }
                });
                return this;
            },
            hide: function() {
                this.elements.forEach(el => {
                    if (el && el.style) {
                        el.style.display = 'none';
                    }
                });
                return this;
            },
            find: function(selector) {
                const foundElements = [];
                this.elements.forEach(el => {
                    if (el && typeof el.querySelectorAll === 'function') {
                        foundElements.push(...Array.from(el.querySelectorAll(selector)));
                    }
                });
                return createJQueryLike(foundElements);
            },
            css: function(property, value) {
                this.elements.forEach(el => {
                    if (el && el.style) {
                        el.style[property] = value;
                    }
                });
                return this;
            },
            on: function(eventName, handler) {
                // Determine the native event name for 'hidden.bs.modal'
                const nativeEventName = eventName === 'hidden.bs.modal' ? 'hidden' : eventName;
                
                this.elements.forEach(el => {
                    if (el && typeof el.addEventListener === 'function') {
                        el.addEventListener(nativeEventName, handler);
                    }
                });
                return this;
            }
        };
    };

    frappe.twint.processPayment = function(amount, reference, type, docname, options = {}) {

        // Default options
        options = Object.assign({
            onSuccess: () => {},
            onError: () => {},
            onCancel: () => {}
        }, options);

        // Determine if we are in the frontend or backend
        const isFrontend = !frappe.ui.form.make_control;
        
        // Function to create a jQuery-like wrapper
        const createJQueryLike = (elements) => {
            return {
                length: elements ? elements.length : 0,
                elements: elements || [],
                addClass: function(className) {
                    this.elements.forEach(el => {
                        if (el && el.classList && typeof el.classList.add === 'function') {
                            el.classList.add(className);
                        }
                    });
                    return this;
                },
                removeClass: function(className) {
                    this.elements.forEach(el => {
                        if (el && el.classList && typeof el.classList.remove === 'function') {
                            el.classList.remove(className);
                        }
                    });
                    return this;
                },
                html: function(content) {
                    this.elements.forEach(el => {
                        if (el && typeof el.innerHTML !== 'undefined') {
                            el.innerHTML = content;
                        }
                    });
                    return this;
                },
                text: function(content) {
                    if (content === undefined) {
                        return this.elements[0] && typeof this.elements[0].textContent !== 'undefined' ? this.elements[0].textContent : '';
                    }
                    this.elements.forEach(el => {
                        if (el && typeof el.textContent !== 'undefined') {
                            el.textContent = content;
                        }
                    });
                    return this;
                },
                attr: function(name, value) {
                    if (value === undefined) {
                        return this.elements[0] && typeof this.elements[0].getAttribute === 'function' ? this.elements[0].getAttribute(name) : null;
                    }
                    this.elements.forEach(el => {
                        if (el && typeof el.setAttribute === 'function') {
                            el.setAttribute(name, value);
                        }
                    });
                    return this;
                },
                show: function() {
                    this.elements.forEach(el => {
                        if (el && el.style) {
                            el.style.display = '';
                        }
                    });
                    return this;
                },
                hide: function() {
                    this.elements.forEach(el => {
                        if (el && el.style) {
                            el.style.display = 'none';
                        }
                    });
                    return this;
                },
                find: function(selector) {
                    const foundElements = [];
                    this.elements.forEach(el => {
                        if (el && typeof el.querySelectorAll === 'function') {
                            foundElements.push(...Array.from(el.querySelectorAll(selector)));
                        }
                    });
                    return createJQueryLike(foundElements);
                },
                css: function(property, value) {
                    this.elements.forEach(el => {
                        if (el && el.style) {
                            el.style[property] = value;
                        }
                    });
                    return this;
                },
                on: function(eventName, handler) {
                    // Determine the native event name for 'hidden.bs.modal'
                    const nativeEventName = eventName === 'hidden.bs.modal' ? 'hidden' : eventName;
                    
                    this.elements.forEach(el => {
                        if (el && typeof el.addEventListener === 'function') {
                            el.addEventListener(nativeEventName, handler);
                        }
                    });
                    return this;
                }
            };
        };

        let dialogContent = `
            <div class="twint-payment-container">
                <img class="twint-line" src="/assets/payments/images/twint/line.png">
                <div class="payment-status-container"></div>
                <div id="twint-app-button-top" style="margin: 15px 0 0 0; text-align: center; display: none;">
                    <button id="open-twint-selector-btn" class="btn btn-primary btn-lg" style="width: 100%; padding: 12px; font-size: 16px; background-color: #FF0039; border-color: #FF0039; max-width: 550px;">
                        ${__('Select your TWINT application')}
                    </button>
                    <div style="display: flex; align-items: center; margin: 20px auto 20px;">
                        <div style="flex-grow: 1; height: 2px; background-color: #ddd;"></div>
                        <div style="margin: 0 10px; color: #666; font-size: 14px;">or</div>
                        <div style="flex-grow: 1; height: 2px; background-color: #ddd;"></div>
                    </div>
                </div>
                <div class="payment-content">
                    <div class="qr-section">
                        <div class="qr-code"></div>
                        <div class="pairing-token"></div>
                        <div class="timer-container">
                            <div class="timer-text">${__("Code valid for:")} <span class="timer-value">5:00</span></div>
                            <div class="timer-bar">
                                <div class="timer-progress" style="width: 100%"></div>
                            </div>
                        </div>
                    </div>
                    <div class="payment-info">
                        <div class="amount-section">    
                            <div class="amount-display">CHF ${amount.toFixed(2)}</div>
                        </div>
                        <div class="company-logo-section">
                            <div class="company-logo">
                                <img id="company-logo" src="/assets/payments/images/twint/twint-app-icon.png">
                            </div>
                        </div>
                    </div>
                </div>
                <div class="twint-instructions">
                    <div class="instruction-step">
                        <div class="icon-app"></div>
                        <p>${__("Open the TWINT app on your smartphone.")}</p>
                    </div>
                    <div class="instruction-step">
                        <img src="/assets/payments/images/twint/qr-code-icon.svg" alt="QR Code">
                        <p>${__("Press the QR button at the bottom of the screen.")}</p>
                    </div>
                    <div class="instruction-step">
                        <img src="/assets/payments/images/twint/camera-icon.svg" alt="Camera">
                        <p>${__("Point your smartphone's camera at the QR code. Or select 'Code' and enter the numeric code.")}</p>
                    </div>
                </div>
            </div>
        `;

        let paymentDialog;
        
        if (isFrontend) {
            // Frontend version (similar to auth_dialog.js)
            const dialogHTML = `
                <div class="twint-dialog-overlay">
                    <div class="twint-dialog">
                        <div class="twint-dialog-header">
                            <h3>${__('TWINT Payment')}</h3>
                            <button class="close-btn">&times;</button>
                        </div>
                        <div class="twint-dialog-content">
                            ${dialogContent}
                        </div>
                    </div>
                </div>
            `;

            const dialogContainer = document.createElement('div');
            dialogContainer.innerHTML = dialogHTML;
            document.body.appendChild(dialogContainer);
            
            // Fix for iOS mobile scroll issue
            document.querySelector('.twint-dialog-overlay').addEventListener('touchmove', function(e) {
                e.stopPropagation();
            }, { passive: false });
            
            document.querySelector('.twint-dialog').addEventListener('touchmove', function(e) {
                e.stopPropagation();
            }, { passive: false });

            // Manage the close button
            const closeBtn = dialogContainer.querySelector('.close-btn');
            closeBtn.addEventListener('click', () => {
                dialogContainer.remove();
                options.onCancel();
            });

            // Simulate Frappe Dialog API
            paymentDialog = {
                hide: () => {
                    dialogContainer.remove();
                },
                show: () => {
                    // Dialog is already visible
                },
                get_value: (fieldname) => {
                    const element = dialogContainer.querySelector(`[data-fieldname="${fieldname}"]`);
                    return element ? element.value : null;
                },
                set_value: (fieldname, value) => {
                    const element = dialogContainer.querySelector(`[data-fieldname="${fieldname}"]`);
                    if (element) element.value = value;
                },
                get_field: (fieldname) => {
                    return {
                        df: { fieldname },
                        disp_area: dialogContainer.querySelector(`[data-fieldname="${fieldname}"]`),
                        value: null
                    };
                },
                fields_dict: {},  // Simulate empty fields_dict
                $wrapper: {
                    find: (selector) => {
                        const elements = Array.from(dialogContainer.querySelectorAll(selector));
                        return createJQueryLike(elements);
                    }
                }
            };
            
        } else {
            // Backend
            paymentDialog = new frappe.ui.Dialog({
                title: __('TWINT Payment'),
                fields: [
                    {
                        fieldtype: 'HTML',
                        fieldname: 'payment_content',
                        options: dialogContent
                    }
                ],
                primary_action_label: __('Cancel'),
                primary_action: () => {
                    stopStatusCheck();
                    if (!paymentDialog.orderUuid) {
                        options.onCancel();
                        paymentDialog.hide();
                        return;
                    }

                    frappe.confirm(
                        __("Do you really want to cancel this payment?"),
                        () => {
                            frappe.call({
                                method: 'payments.integrations.twint.api.cancel_web_transaction',
                                args: {
                                    intent_name: paymentDialog.orderUuid
                                },
                                callback: function(r) {
                                    if (r.message && r.message.success) {
                                        frappe.show_alert({
                                            message: __('Payment cancelled successfully'),
                                            indicator: 'red'
                                        });
                                        options.onCancel();
                                    } else {
                                        frappe.msgprint({
                                            title: __('Error'),
                                            message: r.message.message || __('Error cancelling payment'),
                                            indicator: 'red'
                                        });
                                    }
                                    paymentDialog.hide();
                                }
                            });
                        }
                    );
                }
            });
        }

        let statusCheckInterval;
        const stopStatusCheck = () => {
            if (statusCheckInterval) {
                clearInterval(statusCheckInterval);
                statusCheckInterval = null;
            }
        };

        // Phase 11 — two-step pattern aligned on Stripe/Wallee:
        //   STEP 1 : generic Payment Request creation via payment_handler
        //   STEP 2 : TWINT-specific transaction via the new payments pivot
        // The dialog body, QR/timer/status flow remain the legacy design.
        frappe.call({
            method: 'webshop.controllers.payment_handler.create_payment_request',
            args: {
                quotation_id: docname,
                gateway_settings: 'Twint Bridge Settings',
                idempotency_token: (window.checkout_manager && window.checkout_manager.getIdempotencyToken)
                    ? window.checkout_manager.getIdempotencyToken()
                    : ('twint-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8))
            },
            callback: function(step1) {
                if (!step1.message || step1.message.status !== 'success') {
                    paymentDialog.hide();
                    options.onError({ message: (step1.message && step1.message.message) || __('Error creating payment request') });
                    return;
                }
                const payment_request_id = step1.message.payment_request_id;
                frappe.call({
                    method: 'payments.integrations.twint.api.create_web_transaction',
                    args: { payment_request_id: payment_request_id },
                    callback: function(r) {
                        // Adapt new {status, qr_svg, pairing_token, payment_intent, transaction_id}
                        // to the legacy {success, data:{qr_code, pairing_token, order_uuid, ...}}
                        // contract so the rest of this handler stays verbatim.
                        if (r.message && r.message.status === 'success') {
                            r.message = {
                                success: true,
                                data: {
                                    qr_code: r.message.qr_svg || '',           // inline SVG (not base64 PNG anymore)
                                    pairing_token: r.message.pairing_token,
                                    order_uuid: r.message.payment_intent,       // use intent name as dialog's order_uuid
                                    company_logo: null,
                                    company: null,
                                    twint_order_id: r.message.transaction_id,
                                    // Dev-only: site_config.enable_e2e_simulators=true → show
                                    // a "simulate success" button in the dialog.
                                    simulators_enabled: !!r.message.simulators_enabled
                                }
                            };
                        } else if (r.message) {
                            r.message = { success: false, message: r.message.message || __('TWINT transaction failed') };
                        }
                        _legacyCreatePaymentHandler(r);
                    }
                });
            }
        });

        function _legacyCreatePaymentHandler(r) {
                if (r.message && r.message.success && r.message.data) {
                    const transactionData = r.message.data;
                    const qr_code = r.message.data.qr_code;
                    const pairing_token = r.message.data.pairing_token;
                    const order_uuid = r.message.data.order_uuid;
                    const company_logo = r.message.data.company_logo;
                    const company = r.message.data.company;

                    const $qrCode = paymentDialog.$wrapper.find('.qr-code');
                    const $pairingToken = paymentDialog.$wrapper.find('.pairing-token');
                    const $statusContainer = paymentDialog.$wrapper.find('.payment-status-container');
                    const $qrSection = paymentDialog.$wrapper.find('.qr-section');
                    const $amountSection = paymentDialog.$wrapper.find('.amount-section');
                    const $companyLogoSection = paymentDialog.$wrapper.find('.company-logo-section');
                    const $companyLogo = paymentDialog.$wrapper.find('#company-logo');

                    // Save order_uuid
                    paymentDialog.orderUuid = order_uuid;

                    // Update company logo if available
                    if (company_logo) {
                        $companyLogo.attr('src', company_logo);
                    }

                    // Detect platform
                    function detectPlatform() {
                        const userAgent = navigator.userAgent || navigator.vendor || window.opera;
                        
                        // Detection d'Android
                        if (/android/i.test(userAgent)) {
                            return 'android';
                        }
                        
                        // Detection d'iOS
                        if (/iPad|iPhone|iPod/.test(userAgent) && !window.MSStream) {
                            return 'ios';
                        }
                        
                        // Default, consider as desktop
                        return 'desktop';
                    }
                    
                    // Check if on mobile device
                    const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
                    const platform = detectPlatform();
                    console.log('Plateforme détectée:', platform);
                    
                    // Always display QR code first and start the status check
                    displayQRCode();

                    // Dev-only "simulate success" button — only when the site
                    // has `enable_e2e_simulators=true` in site_config. Lets a
                    // human (or our Playwright tests) finalise the checkout
                    // without scanning a real TWINT QR. The backend endpoint
                    // is itself gated by the same flag, so this is safe by
                    // construction in production.
                    if (transactionData.simulators_enabled) {
                        const qrSectionEl = $qrSection.elements && $qrSection.elements[0];
                        if (qrSectionEl) {
                            const simBtnLabel = '⚠️ ' + __('[DEV] Simulate TWINT success');
                            qrSectionEl.insertAdjacentHTML('beforeend', `
                                <button type="button" class="btn btn-warning btn-sm twint-simulate-success-btn"
                                    style="margin-top: 14px; width: 100%; padding: 10px;
                                           background-color: #ffc107; color: #000;
                                           border: 1px solid #c69500; border-radius: 4px;
                                           font-weight: 600; cursor: pointer;">
                                    ${frappe.utils.escape_html(simBtnLabel)}
                                </button>
                            `);
                            const simBtn = qrSectionEl.querySelector('.twint-simulate-success-btn');
                            if (simBtn) {
                                simBtn.addEventListener('click', function () {
                                    simBtn.disabled = true;
                                    simBtn.textContent = __('Simulating…');
                                    frappe.call({
                                        method: 'payments.api.twint.simulate_consumer_success',
                                        args: { intent_name: paymentDialog.orderUuid },
                                        callback: function (sr) {
                                            if (!(sr && sr.message && sr.message.ok)) {
                                                simBtn.disabled = false;
                                                simBtn.textContent = simBtnLabel;
                                                console.error('TWINT simulate failed:', sr && sr.message);
                                            }
                                            // On success the existing status poller picks up the
                                            // succeeded state and drives the redirect to /thank_you.
                                        }
                                    });
                                });
                            }
                        }
                    }

                    if (isMobile) {
                        // Create a deep link for mobile apps
                        if (/iPhone|iPad|iPod/i.test(navigator.userAgent)) {
                            // For iOS devices, use the TWINT scheme
                            frappe.call({
                                method: 'payments.integrations.twint.api.get_ios_schemes',
                                callback: function(r) {
                                    if (r.message && r.message.success && r.message.schemes && r.message.schemes.length > 0) {
                                        // Add the button to the top container
                                        const $topButtonContainer = paymentDialog.$wrapper.find('#twint-app-button-top');
                                        if ($topButtonContainer.length) {
                                            $topButtonContainer.show();
                                        } else {
                                            console.error('Top container not found');
                                        }
                                        
                                        // Configure click event for the button with debug
                                        setTimeout(() => {
                                            const selectorBtn = document.getElementById('open-twint-selector-btn');                                            
                                            if (selectorBtn) {
                                                selectorBtn.addEventListener('click', function(e) {
                                                    console.log('Bouton TWINT cliqué', e);
                                                    console.log('Paramètres:', { 
                                                        schemes: r.message.schemes, 
                                                        pairing_token: pairing_token,
                                                        statusContainer: $statusContainer
                                                    });
                                                    
                                                    try {
                                                        showTwintAppSelector(r.message.schemes, pairing_token);
                                                    } catch (error) {
                                                        console.error('Error displaying TWINT selector:', error);
                                                    }
                                                });
                                                
                                                // Click the button automatically after a delay
                                                setTimeout(() => {
                                                    selectorBtn.click();
                                                }, 800);
                                            }
                                        }, 200);
                                    }
                                }
                            });
                        } else {
                            // For Android, use the TWINT application selector instead of displaying in the status container
                            // We will call the showTwintAppSelector function to display the dialog with the timer
                            showTwintAppSelector(null, pairing_token);
                            
                            // Display the button at the top of the dialog for Android also
                            const $topButtonContainer = paymentDialog.$wrapper.find('#twint-app-button-top');
                            if ($topButtonContainer.length) {
                                $topButtonContainer.show();
                                
                                // Configure click event for the Android button
                                setTimeout(() => {
                                    const selectorBtn = document.getElementById('open-twint-selector-btn');                                    
                                    if (selectorBtn) {
                                        selectorBtn.addEventListener('click', function(e) {
                                            try {
                                                showTwintAppSelector(null, pairing_token);
                                            } catch (error) {
                                                console.error('Error displaying TWINT selector:', error);
                                            }
                                        });
                                    }
                                }, 100);
                            } else {
                                console.error('Top container not found for Android');
                            }
                        }
                    }
                    
                    // Function to show TWINT app selector
                    function showTwintAppSelector(schemes, pairing_token) {
                        // Detect current platform
                        const currentPlatform = detectPlatform();
                        
                        // Verify that the pairing_token is valid (necessary for both platforms)
                        if (!pairing_token) {
                            console.error('Error: pairing_token is invalid', pairing_token);
                            return;
                        }
                        
                        // For iOS, verify that the schemes are valid
                        if (currentPlatform === 'ios' && (!schemes || !Array.isArray(schemes) || schemes.length === 0)) {
                            console.error('Error: schemes are invalid for iOS', schemes);
                            return;
                        }
                        
                        if (currentPlatform === 'ios') {
                            console.log('showTwintAppSelector called with:', schemes.length, 'iOS apps');
                        }
                        
                        // Create a simple app selector directly in the page
                        const selectorDiv = document.createElement('div');
                        selectorDiv.className = 'twint-app-selector-container';
                        selectorDiv.style.position = 'fixed';
                        selectorDiv.style.top = '0';
                        selectorDiv.style.left = '0';
                        selectorDiv.style.width = '100%';
                        selectorDiv.style.height = '100%';
                        selectorDiv.style.backgroundColor = 'rgba(0,0,0,0.7)';
                        selectorDiv.style.zIndex = '9999';
                        selectorDiv.style.display = 'flex';
                        selectorDiv.style.alignItems = 'center';
                        selectorDiv.style.justifyContent = 'center';
                        
                        // For Android, we do not use the app selector like for iOS
                        if (currentPlatform === 'android') {
                            // Format of the Android deep link
                            const androidDeepLink = `intent://payment#Intent;action=ch.twint.action.TWINT_PAYMENT;scheme=twint;S.code=${pairing_token};S.startingOrigin=EXTERNAL_WEB_BROWSER;S.browser_fallback_url=;end`;
                            
                            // Create the content for Android with timer
                            selectorDiv.innerHTML = `
                                <div style="background: white; padding: 20px; border-radius: 8px; max-width: 90%; width: 350px; box-shadow: 0 5px 15px rgba(0,0,0,0.3);">
                                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                                        <h3 style="margin: 0; font-size: 18px;">${__('Redirection vers TWINT')}</h3>
                                        <button id="twint-selector-close" style="background: none; border: none; font-size: 20px; cursor: pointer;">&times;</button>
                                    </div>
                                    
                                    <div style="text-align: center; margin: 20px 0;">
                                        <p>${__('You will be redirected to the TWINT app in')} <span id="twint-countdown">3</span> ${__('seconds')}...</p>
                                    </div>
                                    
                                    <button id="twint-open-now" class="btn btn-primary btn-lg" style="width: 100%; padding: 10px; background-color: #FF0039; color: white; border: none; border-radius: 4px; cursor: pointer;">
                                        ${__('Open now')}
                                    </button>
                                </div>
                            `;
                            
                            // Add the selector to the page
                            document.body.appendChild(selectorDiv);
                            
                            // Handle events
                            const closeBtn = document.getElementById('twint-selector-close');
                            const openNowBtn = document.getElementById('twint-open-now');
                            const countdownEl = document.getElementById('twint-countdown');
                            
                            // Timer for countdown
                            let countdown = 3;
                            
                            if (closeBtn) {
                                closeBtn.addEventListener('click', function() {
                                    console.log('Fermeture du sélecteur');
                                    selectorDiv.remove();
                                    // Stop the timer if the selector is closed
                                    if (window.androidTimer) {
                                        clearInterval(window.androidTimer);
                                    }
                                });
                            }
                            
                            if (openNowBtn) {
                                openNowBtn.addEventListener('click', function() {
                                    console.log('Ouverture immédiate de TWINT');
                                    // Trigger a custom event before redirection
                                    const redirectEvent = new CustomEvent('twintRedirect', {
                                        detail: {
                                            platform: 'android',
                                            pairingToken: pairing_token,
                                            timestamp: new Date().toISOString()
                                        },
                                        bubbles: true,
                                        cancelable: true
                                    });
                                    document.dispatchEvent(redirectEvent);
                                    
                                    // Close the selector after a short delay
                                    setTimeout(() => {
                                        // Close the selector
                                        selectorDiv.remove();                                        
                                        // Open the application
                                        window.location.href = androidDeepLink;
                                    }, 50);
                                });
                            }
                            
                            // Start the countdown
                            window.androidTimer = setInterval(function() {
                                countdown--;
                                if (countdownEl) {
                                    countdownEl.textContent = countdown;
                                }
                                
                                if (countdown <= 0) {
                                    clearInterval(window.androidTimer);                                    
                                    // Trigger a custom event before automatic redirection
                                    const redirectEvent = new CustomEvent('twintRedirect', {
                                        detail: {
                                            platform: 'android',
                                            pairingToken: pairing_token,
                                            automatic: true,
                                            timestamp: new Date().toISOString()
                                        },
                                        bubbles: true,
                                        cancelable: true
                                    });
                                    document.dispatchEvent(redirectEvent);
                                    // Close the selector after a short delay
                                    setTimeout(() => {
                                        // Close the selector
                                        selectorDiv.remove();                                        
                                        // Open the application
                                        window.location.href = androidDeepLink;
                                    }, 50);
                                }
                            }, 1000);
                            
                            return; // Exit the function for Android
                        }
                        
                        // For iOS, continue with the app selector
                        // Create the content of the selector
                        let optionsHtml = `<option value="" disabled selected>${__('Other applications')}</option>`;
                        schemes.forEach(scheme => {
                            // Remove 'TWINT' from the application name
                            let displayName = scheme.display_name.replace(/\s*TWINT\s*/gi, '');
                            optionsHtml += `<option value="${scheme.scheme}">${displayName}</option>`;
                        });
                        
                        // Prepare the icons of the most used banks
                        const bankIcons = [
                            { name: 'UBS', icon: '/assets/payments/images/twint/ubs.svg', scheme: '' },
                            { name: 'Raiffeisen', icon: '/assets/payments/images/twint/raiffeisen.svg', scheme: '' },
                            { name: 'PostFinance', icon: '/assets/payments/images/twint/pf.svg', scheme: '' },
                            { name: 'ZKB', icon: '/assets/payments/images/twint/zkb.svg', scheme: '' },
                            { name: 'Credit Suisse', icon: '/assets/payments/images/twint/cs.svg', scheme: '' },
                            { name: 'BCV', icon: '/assets/payments/images/twint/bcv.svg', scheme: '' }
                        ];
                        
                        // Associate the schemas with the corresponding banks
                        schemes.forEach(scheme => {
                            const bankName = scheme.display_name.toLowerCase();
                            bankIcons.forEach(bank => {
                                if (bankName.includes(bank.name.toLowerCase())) {
                                    bank.scheme = scheme.scheme;
                                }
                            });
                        });
                        
                        // Create the HTML for the bank icons
                        let bankIconsHtml = '';
                        bankIcons.forEach(bank => {
                            if (bank.scheme) {
                                bankIconsHtml += `
                                    <div class="twint-bank-icon" data-scheme="${bank.scheme}" style="cursor: pointer; text-align: center; margin-bottom: 15px;">
                                        <img src="${bank.icon}" alt="${bank.name}" style="width: 60px; height: 60px; border-radius: 10px; border: 1px solid #eee; padding: 5px; background-color: white;">
                                        <div style="font-size: 12px; margin-top: 5px;">${bank.name}</div>
                                    </div>
                                `;
                            }
                        });
                        
                        selectorDiv.innerHTML = `
                            <div style="background: white; padding: 20px; border-radius: 8px; max-width: 90%; width: 350px; box-shadow: 0 5px 15px rgba(0,0,0,0.3);">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                                    <h3 style="margin: 0; font-size: 18px;">${__('Choose your TWINT application')}</h3>
                                    <button id="twint-selector-close" style="background: none; border: none; font-size: 20px; cursor: pointer;">&times;</button>
                                </div>
                                
                                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 20px;">
                                    ${bankIconsHtml}
                                </div>
                                
                                <div style="display: flex; align-items: center; margin: 15px 0;">
                                    <div style="flex-grow: 1; height: 2px; background-color: #ddd;"></div>
                                    <div style="margin: 0 10px; color: #666; font-size: 14px;">${__('or')}</div>
                                    <div style="flex-grow: 1; height: 2px; background-color: #ddd;"></div>
                                </div>
                                
                                <div style="margin-bottom: 20px;">
                                    <select id="twint-app-select" style="width: 100%; padding: 10px 15px; border: 1px solid #ddd; border-radius: 4px; background-color: white; font-size: 14px; appearance: menulist; height: 40px;">
                                        ${optionsHtml}
                                    </select>
                                </div>
                            </div>
                        `;
                        
                        // Add the selector to the page
                        document.body.appendChild(selectorDiv);
                        
                        // Handle events
                        const closeBtn = document.getElementById('twint-selector-close');
                        const appSelect = document.getElementById('twint-app-select');
                        const bankIconElements = document.querySelectorAll('.twint-bank-icon');
                        
                        // Check that the elements exist
                        if (!closeBtn || !appSelect) {
                            console.error('Error: selector elements not found', {
                                closeBtn: !!closeBtn,
                                appSelect: !!appSelect
                            });
                            return;
                        }
                        
                        // Close event
                        closeBtn.addEventListener('click', function() {
                            selectorDiv.remove();
                        });
                        
                        // Function to open the TWINT application with the selected schema (iOS only)
                        function openTwintApp(selectedScheme) {
                            
                            // Format of the iOS deep link with JSON parameters for PF Finance
                            // twint-issuer7://applinks/?al_applink_data={"app_action_type":"TWINT_PAYMENT","extras":{"code":"16586"},"referer_app_link":{"target_url":"","url":"","app_name":"EXTERNAL_WEB_BROWSER"},"version":"6.0"}
                            
                            // Replace the code with the pairing_token
                            const appLinkData = {
                                "app_action_type": "TWINT_PAYMENT",
                                "extras": {
                                    "code": pairing_token
                                },
                                "referer_app_link": {
                                    "target_url": "",
                                    "url": "",
                                    "app_name": "EXTERNAL_WEB_BROWSER"
                                },
                                "version": "6.0"
                            };
                            
                            // Format for iOS (format provided by the user)
                            const deepLinkUrl = `${selectedScheme}//applinks/?al_applink_data=${JSON.stringify(appLinkData)}`;
                            console.log('Deep link URL:', deepLinkUrl);
                            
                            // Trigger a custom event before redirection
                            const redirectEvent = new CustomEvent('twintRedirect', {
                                detail: {
                                    platform: 'ios',
                                    scheme: selectedScheme,
                                    pairingToken: pairing_token,
                                    timestamp: new Date().toISOString()
                                },
                                bubbles: true,
                                cancelable: true
                            });
                            document.dispatchEvent(redirectEvent);
                            
                            // Close the selector after a short delay
                            setTimeout(() => {
                                // Close the selector
                                selectorDiv.remove();
                                
                                // Open the application
                                window.location.href = deepLinkUrl;
                            }, 50);
                        }
                        
                        // Add click events to bank icons
                        bankIconElements.forEach(icon => {
                            icon.addEventListener('click', function() {
                                const scheme = this.getAttribute('data-scheme');
                                if (scheme) {
                                    openTwintApp(scheme);
                                }
                            });
                        });
                        
                        // Event to open the application when the selection changes
                        appSelect.addEventListener('change', function() {
                            const selectedScheme = this.value;
                            if (selectedScheme) {
                                openTwintApp(selectedScheme);
                            }
                        });
                    }
                    
                    // Function to display QR code for non-mobile devices
                    function displayQRCode() {
                        // Clear the status container first since it's now placed at the top
                        $statusContainer.html(''); // Use html('') instead of empty()
                        
                        if (qr_code && pairing_token) {
                            // Phase 11 — qr_code is now an inline SVG string (not a base64 PNG),
                            // generated server-side by Python `qrcode` lib from the pairing token.
                            if (typeof qr_code === 'string' && qr_code.trim().indexOf('<svg') === 0) {
                                $qrCode.html(qr_code);
                            } else {
                                // Defensive fallback if endpoint ever switches back to a data URL.
                                $qrCode.html(`<img src="${qr_code}" alt="TWINT QR Code">`);
                            }
                            $pairingToken.text(pairing_token);
                            setTimeout(() => $qrSection.addClass('visible'), 300);
                            setTimeout(() => $amountSection.addClass('visible'), 400);
                            setTimeout(() => $companyLogoSection.addClass('visible'), 500);

                            // Manage countdown
                            let timeLeft = 300; 
                            const $timerValue = $qrSection.find('.timer-value');
                            const $timerProgress = $qrSection.find('.timer-progress');

                            const timerInterval = setInterval(() => {
                                timeLeft--;
                                const minutes = Math.floor(timeLeft / 60);
                                const seconds = timeLeft % 60;
                                $timerValue.text(`${minutes}:${seconds.toString().padStart(2, '0')}`);
                                $timerProgress.css('width', `${(timeLeft / 300) * 100}%`);

                                if (timeLeft <= 0) {
                                    clearInterval(timerInterval);
                                    stopStatusCheck();
                                    $statusContainer.html(`
                                        <div class="alert alert-danger">
                                            ${__("The QR code has expired. Please refresh the page to generate a new code.")}
                                        </div>
                                    `);
                                }
                            }, 1000);
                        }
                    }

                    // Display initial status
                    $statusContainer.html(`
                        <div class="payment-status">
                            <div class="loading-spinner"></div>
                            <div class="text-muted" style="margin-top: 10px;">
                                ${__('Waiting for payment...')}
                            </div>
                        </div>
                    `);

                    // Status flow — Phase 11:
                    //   PRIMARY: SocketIO realtime push from backend poll job
                    //            (payment.intent.<id>.updated events).
                    //   FALLBACK: poll get_intent_state every 5s in case the
                    //            socket connection drops mid-flow.
                    // ``order_uuid`` here is the Payment Intent name (set by the
                    // adapter in _legacyCreatePaymentHandler) — we subscribe and
                    // poll the unified FSM status (lowercase: succeeded/failed/canceled).

                    const _onIntentUpdate = function(data) {
                        if (!data || data.intent_name !== order_uuid) return;
                        _applyStatus(data.status, data.redirect_to, data.error_message);
                    };
                    if (frappe.realtime && frappe.realtime.on) {
                        frappe.realtime.on('payment.intent.' + order_uuid + '.updated', _onIntentUpdate);
                    }

                    function _applyStatus(status, redirect_to, error_message) {
                        if (status === 'succeeded') {
                            stopStatusCheck();
                            if (frappe.realtime && frappe.realtime.off) {
                                frappe.realtime.off('payment.intent.' + order_uuid + '.updated', _onIntentUpdate);
                            }
                            $statusContainer.html(
                                '<div class="alert alert-success">' + __('Payment was successful') + '</div>'
                            );
                            if (window.location.pathname === '/checkout' && type === 'Quotation') {
                                // Backend has already called handle_payment_success and
                                // attached the redirect_to URL to the realtime event.
                                setTimeout(function() {
                                    frappe.show_alert({ message: __('Payment successful'), indicator: 'green' });
                                }, 1500);
                                const fallback = '/thank_you?payment_intent=' + encodeURIComponent(order_uuid);
                                setTimeout(function() {
                                    paymentDialog.hide();
                                    window.location.href = redirect_to || fallback;
                                }, 1800);
                            } else {
                                setTimeout(function() {
                                    paymentDialog.hide();
                                    frappe.show_alert({ message: __('Payment successful'), indicator: 'green' });
                                }, 1500);
                                options.onSuccess({ status: status, redirect_to: redirect_to });
                            }
                        } else if (status === 'failed' || status === 'canceled') {
                            stopStatusCheck();
                            if (frappe.realtime && frappe.realtime.off) {
                                frappe.realtime.off('payment.intent.' + order_uuid + '.updated', _onIntentUpdate);
                            }
                            $statusContainer.html(
                                '<div class="alert alert-danger">' +
                                    (error_message || __('Payment failed or was cancelled')) +
                                '</div>'
                            );
                            options.onError({ status: status, message: error_message });
                        }
                    }

                    // Fallback poll (slow — SocketIO is the primary signal).
                    statusCheckInterval = setInterval(function() {
                        frappe.call({
                            method: 'payments.integrations.twint.api.get_intent_state',
                            args: { intent_name: order_uuid },
                            callback: function(r) {
                                if (!r.message) return;
                                _applyStatus(r.message.status, null, r.message.error_message);
                            }
                        });
                    }, 5000);
                } else {
                    paymentDialog.hide();
                    frappe.msgprint({
                        title: __('Error'),
                        indicator: 'red',
                        message: r.message.message
                    });
                    options.onError(r.message);
                }
        }

        paymentDialog.show();

        // Create jQuery-like wrapper if necessary
        if (!frappe.ui || !frappe.ui.form || !frappe.ui.form.make_control) {
            const wrapperElement = paymentDialog.$wrapper instanceof HTMLElement ? paymentDialog.$wrapper : document.querySelector('.twint-dialog');
            paymentDialog.$wrapper = createJQueryLike([wrapperElement]);
        }
        
        if (paymentDialog.$wrapper) {
            paymentDialog.$wrapper.addClass('twint-modal-dialog');
        }

        // Get the dialog wrapper
        const dialogWrapper = paymentDialog.$wrapper.elements ? paymentDialog.$wrapper.elements[0] : paymentDialog.$wrapper;
        const closeButton = dialogWrapper && typeof dialogWrapper.querySelector === 'function' ? 
            dialogWrapper.querySelector('.twint-dialog .close-btn') : 
            document.querySelector('.twint-dialog .close-btn');
            

        if (closeButton) {
            closeButton.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                
                if (frappe.ui && frappe.ui.form && frappe.ui.form.make_control) {
                    // Frontend environment
                    frappe.confirm(
                        __("Do you really want to cancel this payment?"),
                        () => {
                            frappe.call({
                                method: 'payments.integrations.twint.api.cancel_web_transaction',
                                args: {
                                    intent_name: paymentDialog.orderUuid
                                },
                                callback: function(r) {
                                    if (r.message && r.message.success) {
                                        frappe.show_alert({
                                            message: __('Payment cancelled successfully'),
                                            indicator: 'red'
                                        });
                                        window.location.reload();
                                    } else {
                                        frappe.msgprint({
                                            title: __('Error'),
                                            message: r.message.message || __('Error cancelling payment'),
                                            indicator: 'red'
                                        });
                                    }
                                    paymentDialog.hide();
                                    stopStatusCheck();
                                }
                            });
                        }
                    );
                } else {
                    // Backend environment
                    if (confirm(__("Do you really want to cancel this payment?"))) {
                        frappe.call({
                            method: 'payments.integrations.twint.api.cancel_web_transaction',
                            args: {
                                intent_name: paymentDialog.orderUuid
                            },
                            callback: function(r) {
                                if (r.message && r.message.success) {
                                    alert(__('Payment cancelled successfully'));
                                    if (options.onCancel) {
                                        options.onCancel();
                                    }
                                } else {
                                    alert(__('Error cancelling payment'));
                                }
                                paymentDialog.hide();
                                stopStatusCheck();
                            }
                        });
                    }
                }
            });
        } else {
            console.warn("Close button not found in dialog");
        }

        // Clean up
        if (frappe.ui && frappe.ui.form && frappe.ui.form.make_control) {
            // Frontend environment - use standard jQuery event
            paymentDialog.$wrapper.on('hidden.bs.modal', function() {
                stopStatusCheck();
            });
        } else {
            // Backend
            const dialogElement = paymentDialog.$wrapper.elements ? paymentDialog.$wrapper.elements[0] : paymentDialog.$wrapper;
            if (dialogElement && typeof dialogElement.addEventListener === 'function') {
                dialogElement.addEventListener('hidden', function() {
                    stopStatusCheck();
                });
            }
        }

    };
})();