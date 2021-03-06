"""TensorFlow implementation for photo restoration with conv and deconv
followed by discriminator"""

from __future__ import absolute_import, division, print_function

import math
import os
import pickle
import numpy as np
import scipy.misc
from scipy.misc import imsave
from progressbar import ETA, Bar, Percentage, ProgressBar

import tensorflow as tf
import prettytensor as pt  # https://github.com/google/prettytensor

import input_data
from deconv import deconv2d_hack
from ops import deconv2d
import ops

flags = tf.flags
# logging = tf.logging
flags.DEFINE_integer("image_size", 64, "The size of image to use [64]")
flags.DEFINE_integer("batch_size", 1024, "batch size")
flags.DEFINE_integer("updates_per_epoch", 100, "number of updates per epoch")
flags.DEFINE_integer("max_epoch", 100, "max epoch")
flags.DEFINE_float("r_learning_rate", 0.001, "learning rate")
flags.DEFINE_string("working_directory", "data/", "directory where your data is")
flags.DEFINE_string("results_directory", "results/", "directory where to save your evaluation results")
flags.DEFINE_string("checkpoint_dir", "checkpoint", "Directory name to save the checkpoints [checkpoint]")
flags.DEFINE_integer("hidden_size", 4096, "size of the hidden VAE unit")
# D
flags.DEFINE_float("beta1", 0.5, "Momentum term of adam [0.8]")
flags.DEFINE_float("d_learning_rate", 0.0002, "Learning rate of for adam [0.0002]")
flags.DEFINE_integer("df_dim", 64, "Dimension of discriminator filters in first conv layer. [64]")

FLAGS = flags.FLAGS


def encoder(input_tensor, reuse=False):
    '''Create encoder network.
    Args:
        input_tensor: a batch of flattened images [batch_size, 64*64]
    Returns:
        A tensor that expresses the encoder network
    '''
    if reuse: tf.get_variable_scope().reuse_variables()

    return (pt.wrap(input_tensor).
            reshape([FLAGS.batch_size, FLAGS.image_size, FLAGS.image_size, 1]).
            conv2d(5, 32, stride=2).
            conv2d(5, 64, stride=2).
            conv2d(5, 128, edges='VALID').
            flatten()).tensor
    # fully_connected(FLAGS.hidden_size * 2, activation_fn=None)).tensor


def decoder(input_tensor=None, reuse=False):
    '''Create decoder network.
        If input tensor is provided then decodes it, otherwise samples from 
        a sampled vector.
    Args:
        input_tensor: a batch of vectors to decode
    Returns:
        A tensor that expresses the decoder network
    '''
    if reuse: tf.get_variable_scope().reuse_variables()
    epsilon = tf.random_normal([FLAGS.batch_size, FLAGS.hidden_size])
    # output_tensor = tf.random_normal([FLAGS.batch_size, FLAGS.image_size,FLAGS.image_size,1])
    if input_tensor is None:
        mean = None
        stddev = None
        input_sample = epsilon
    else:
        mean = input_tensor[:, :FLAGS.hidden_size]
        stddev = tf.sqrt(tf.exp(input_tensor[:, :FLAGS.hidden_size]))
        input_sample = mean + epsilon * stddev

    return (pt.wrap(input_sample).
            reshape([FLAGS.batch_size, 1, 1, FLAGS.hidden_size]).
            deconv2d_hack(3, 128, edges='VALID').
            deconv2d_hack(3, 128, edges='VALID').
            deconv2d_hack(3, 128, edges='VALID').
            deconv2d_hack(3, 128, edges='VALID').
            deconv2d_hack(3, 128, edges='VALID').
            deconv2d_hack(2, 128, edges='VALID').
            deconv2d_hack(5, 64, edges='VALID').
            deconv2d_hack(5, 32, stride=2).
            deconv2d_hack(5, 1, stride=2, activation_fn=tf.nn.sigmoid).
            flatten()).tensor, mean, stddev


def discriminator(image, reuse=False):
    d_bn1 = ops.batch_norm(FLAGS.batch_size, name='d_bn1')
    d_bn2 = ops.batch_norm(FLAGS.batch_size, name='d_bn2')
    d_bn3 = ops.batch_norm(FLAGS.batch_size, name='d_bn3')
    image = tf.reshape(image, [FLAGS.batch_size, FLAGS.image_size, FLAGS.image_size, 1])
    if reuse: tf.get_variable_scope().reuse_variables()
    h0 = ops.lrelu(ops.conv2d(image, FLAGS.df_dim, name='d_h0_conv'))
    h1 = ops.lrelu(d_bn1(ops.conv2d(h0, FLAGS.df_dim * 2, name='d_h1_conv')))
    h2 = ops.lrelu(d_bn2(ops.conv2d(h1, FLAGS.df_dim * 4, name='d_h2_conv')))
    h3 = ops.lrelu(d_bn3(ops.conv2d(h2, FLAGS.df_dim * 8, name='d_h3_conv')))
    h4 = ops.linear(tf.reshape(h3, [FLAGS.batch_size, -1]), 1, 'd_h3_lin')
    return tf.nn.sigmoid(h4)


def get_reconstruction_cost(output_tensor, target_tensor, mask=None, epsilon=1e-8):
    '''Reconstruction loss
    Cross entropy reconstruction loss
    Args:
        output_tensor: tensor produces by decoder 
        target_tensor: the target tensor that we want to reconstruct
        epsilon:
    '''

    if mask:
        masked_output_tensor, masked_target_tensor = tf.mul(output_tensor, mask), tf.mul(target_tensor, mask)
        nonmasked_output_tensor, nonmasked_target_tensor = tf.mul(output_tensor, tf.sub(tf.ones_like(mask),mask)),\
                                                            tf.mul(target_tensor, tf.sub(tf.ones_like(mask),mask))
        masked_l2_cost = tf.nn.l2_loss(masked_target_tensor - masked_output_tensor) * 2
        nonmasked_l2_cost = tf.nn.l2_loss(nonmasked_target_tensor - nonmasked_output_tensor) * 2

        return 5*masked_l2_cost + nonmasked_l2_cost

    else:
        lr_cost = tf.reduce_sum(-target_tensor * tf.log(output_tensor + epsilon) -
                                (1.0 - target_tensor) * tf.log(1.0 - output_tensor + epsilon))
        l2_cost = tf.nn.l2_loss(target_tensor - output_tensor) * 2
        return l2_cost


if __name__ == "__main__":
    # prep
    data_directory = os.path.join(FLAGS.working_directory, "celebACropped")
    if not os.path.exists(FLAGS.checkpoint_dir): os.makedirs(FLAGS.checkpoint_dir)
    if not os.path.exists(data_directory): os.makedirs(data_directory)
    celebACropped = input_data.read_data_sets(data_directory)
    loss_book_keeper = []
    # build model
    input_tensor = tf.placeholder(tf.float32, [FLAGS.batch_size, FLAGS.image_size * FLAGS.image_size],
                                  name="input_tensor")
    ground_truth_tensor = tf.placeholder(tf.float32, [FLAGS.batch_size, FLAGS.image_size * FLAGS.image_size],
                                         name="gt_tensor")
    mask_tensor = tf.placeholder(tf.float32, [FLAGS.batch_size, FLAGS.image_size * FLAGS.image_size],
                                 name="mask_tensor")
    sampled_tensor = tf.placeholder(tf.float32, [FLAGS.batch_size, FLAGS.image_size * FLAGS.image_size],
                                    name='sampled_tensor')
    output_tensor = tf.placeholder(tf.float32, [FLAGS.batch_size, FLAGS.image_size * FLAGS.image_size],
                                   name='output_tensor')
    restored_tensor = tf.placeholder(tf.float32, [FLAGS.batch_size, FLAGS.image_size * FLAGS.image_size],
                                     name='restored_tensor')
    with pt.defaults_scope(activation_fn=tf.nn.elu,
                           batch_normalize=True,
                           learned_moments_update_rate=0.0003,
                           variance_epsilon=0.001,
                           scale_after_normalization=True):#multiply by gamma?
        # https://github.com/google/prettytensor/blob/master/prettytensor/pretty_tensor_image_methods.py
        #https://github.com/Lasagne/Lasagne/issues/141
        with pt.defaults_scope(phase=pt.Phase.train):
            with tf.variable_scope("model") as scope:
                output_tensor, mean, stddev = decoder(encoder(input_tensor))
                D = discriminator(ground_truth_tensor)
                D_ = discriminator(tf.add(input_tensor, tf.mul(output_tensor, mask_tensor)), reuse=True)

        with pt.defaults_scope(phase=pt.Phase.test):
            with tf.variable_scope("model", reuse=True) as scope:
                sampled_tensor, _, _ = decoder(encoder(input_tensor))
                restored_tensor = tf.add(tf.mul(input_tensor, tf.sub(tf.ones_like(mask_tensor),mask_tensor)),
                                         tf.mul(sampled_tensor, mask_tensor))
    # Restorer reconstruct
    rec_loss = get_reconstruction_cost(output_tensor, ground_truth_tensor,
                                       mask=None, epsilon=1e-12)
    r_loss = rec_loss  # +g_loss
    r_optim = tf.train.AdamOptimizer(FLAGS.r_learning_rate, epsilon=1e-12)
    r_train = pt.apply_optimizer(r_optim, losses=[r_loss])

    # Discriminator
    d_sum = tf.histogram_summary("d", D)
    d__sum = tf.histogram_summary("d_", D_)
    d_loss_real = ops.binary_cross_entropy_with_logits(tf.ones_like(D), D)
    d_loss_fake = ops.binary_cross_entropy_with_logits(tf.zeros_like(D_), D_)
    d_loss_real_sum = tf.scalar_summary("d_loss_real", d_loss_real)
    d_loss_fake_sum = tf.scalar_summary("d_loss_fake", d_loss_fake)
    d_loss = d_loss_real + d_loss_fake
    d_loss_sum = tf.scalar_summary("d_loss", d_loss)
    t_vars = tf.trainable_variables()
    d_vars = [var for var in t_vars if 'd_' in var.name]
    d_optim = tf.train.AdamOptimizer(FLAGS.d_learning_rate, beta1=FLAGS.beta1) \
        .minimize(d_loss, var_list=d_vars)

    # Restorer adverse Discriminator
    g_loss = ops.binary_cross_entropy_with_logits(tf.ones_like(D_), D_)
    g_optim = tf.train.AdamOptimizer(FLAGS.r_learning_rate, epsilon=1.0)
    g_train = pt.apply_optimizer(g_optim, losses=[g_loss])

    # General stuff
    init = tf.initialize_all_variables()
    saver = tf.train.Saver()

    # run as session
    with tf.Session() as sess:
        sess.run(init)
        for epoch in range(FLAGS.max_epoch):
            training_loss = 0.0
            widgets = ["epoch #%d|" % epoch, Percentage(), Bar(), ETA()]
            pbar = ProgressBar(FLAGS.updates_per_epoch, widgets=widgets)
            pbar.start()
            for i in range(FLAGS.updates_per_epoch):
                pbar.update(i)
                mask, x_masked, x_ground_truth = celebACropped.train.next_batch(FLAGS.batch_size)
                # print (mask.shape)
                # print ('reconstruct')
                # Restorer reconstruct
                _, loss_value = sess.run(fetches=[r_train, r_loss],
                                         feed_dict={input_tensor: x_masked,
                                                    mask_tensor: mask,
                                                    ground_truth_tensor: x_ground_truth})
                # print ('discriminator')
                # discriminator
                _, summary_str = sess.run(fetches=[d_optim, d_sum],
                                          feed_dict={input_tensor: x_masked,
                                                     mask_tensor: mask,
                                                     ground_truth_tensor: x_ground_truth})
                # print ('restorer adverse discriminator')
                # Restorer adverse discriminator
                _, g_loss_value = sess.run(fetches=[g_train, g_loss],
                                           feed_dict={input_tensor: x_masked,
                                                      mask_tensor: mask})
                # update restorer again in case discriminator learns too fast that they can't reach equilibrium state
                # _, loss_value = sess.run(fetches=[r_train, r_loss],
                #                          feed_dict={input_tensor: x_masked, ground_truth_tensor: x_ground_truth})
                errG_recons = loss_value/float(FLAGS.batch_size * (FLAGS.image_size ** 2))
                errD_fake = d_loss_fake.eval({input_tensor: x_masked,mask_tensor: mask})
                errD_real = d_loss_real.eval({ground_truth_tensor: x_ground_truth})
                errG = g_loss_value / float(FLAGS.batch_size * (FLAGS.image_size ** 2))
                print("Epoch: [%2d] update batch: [%4d] ,r_loss: %.8f, d_loss: %.8f, g_loss: %.8f" \
                      % (epoch, i,errG_recons ,errD_fake + errD_real, errG))
                loss_book_keeper.append((epoch, i, errD_fake + errD_real, errG))

            if epoch % 5 == 0:
                print("reached %5==0, save and evaluate results")
                saver.save(sess, save_path=os.path.join(FLAGS.checkpoint_dir, 'vae'), global_step=epoch)
                output_loss_keeper = open('loss.pkl', 'wb')
                pickle.dump(loss_book_keeper, output_loss_keeper)
                output_loss_keeper.close()

                mask, x_masked, x_ground_truth = celebACropped.test.next_batch(FLAGS.batch_size)
                imgs,sampled_img = sess.run(fetches=[restored_tensor,sampled_tensor], feed_dict={input_tensor: x_masked,mask_tensor:mask})
                for k in range(FLAGS.batch_size):
                    imgs_folder = os.path.join(FLAGS.results_directory, 'imgs' + str(epoch))
                    if not os.path.exists(imgs_folder): os.makedirs(imgs_folder)
                    imsave(os.path.join(imgs_folder, '%d' + '_restored.png') % k,
                           imgs[k].reshape(FLAGS.image_size, FLAGS.image_size))
                    imsave(os.path.join(imgs_folder, '%d' + '_masked.png') % k,
                           x_masked[k].reshape(FLAGS.image_size, FLAGS.image_size))
                    imsave(os.path.join(imgs_folder, '%d.' + '_ground_truth.png') % k,
                           x_ground_truth[k].reshape(FLAGS.image_size, FLAGS.image_size))
                    imsave(os.path.join(imgs_folder, '%d' + '_sampled_img.png') % k,
                           sampled_img[k].reshape(FLAGS.image_size, FLAGS.image_size))
                    imsave(os.path.join(imgs_folder, '%d' + '_pure_mask.png') % k,
                           mask[k].reshape(FLAGS.image_size, FLAGS.image_size))
